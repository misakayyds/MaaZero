package download

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

var androidCertDirs = []string{
	"/apex/com.android.conscrypt/cacerts",
	"/system/etc/security/cacerts",
}

var fallbackDNSServers = []string{
	"223.5.5.5:53",
	"119.29.29.29:53",
}

const maxCertFileBytes = 256 << 10

var (
	certPoolOnce  sync.Once
	certPoolCache *x509.CertPool
	certPoolAdded int
)

func cachedAndroidCertPool() (*x509.CertPool, int) {
	certPoolOnce.Do(func() {
		certPoolCache, certPoolAdded = loadExtraCertPool(androidCertDirs...)
	})
	return certPoolCache, certPoolAdded
}

// DefaultTransport returns an HTTP transport safe for static CGO_ENABLED=0
// ARM64 Linux builds running directly on Android, where /etc/resolv.conf and
// /etc/ssl/certs are absent outside a desktop rootfs.
func DefaultTransport() *http.Transport {
	var tr *http.Transport
	if base, ok := http.DefaultTransport.(*http.Transport); ok && base != nil {
		tr = base.Clone()
	} else {
		tr = &http.Transport{
			Proxy:             http.ProxyFromEnvironment,
			ForceAttemptHTTP2: true,
		}
	}
	if runtime.GOOS != "linux" {
		return tr
	}
	if pool, added := cachedAndroidCertPool(); added > 0 {
		tlsCfg := &tls.Config{
			MinVersion: tls.VersionTLS12,
			RootCAs:    pool,
		}
		if tr.TLSClientConfig != nil {
			tlsCfg = tr.TLSClientConfig.Clone()
			if tlsCfg.MinVersion < tls.VersionTLS12 {
				tlsCfg.MinVersion = tls.VersionTLS12
			}
			tlsCfg.RootCAs = pool
		}
		tr.TLSClientConfig = tlsCfg
	}
	if !hasResolvNameserver("/etc/resolv.conf") {
		dialer := &net.Dialer{
			Timeout:   15 * time.Second,
			KeepAlive: 30 * time.Second,
			Resolver:  newFallbackResolver(cachedAndroidDNSProps),
		}
		tr.DialContext = dialer.DialContext
	}
	return tr
}

func loadExtraCertPool(dirs ...string) (*x509.CertPool, int) {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	added := 0
	for _, dir := range dirs {
		entries, err := os.ReadDir(dir)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if entry.IsDir() {
				continue
			}
			path := filepath.Join(dir, entry.Name())
			info, err := os.Stat(path)
			if err != nil || !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > maxCertFileBytes {
				continue
			}
			raw, err := os.ReadFile(path)
			if err != nil {
				continue
			}
			if pool.AppendCertsFromPEM(raw) {
				added++
				continue
			}
			if cert, err := x509.ParseCertificate(raw); err == nil {
				pool.AddCert(cert)
				added++
			}
		}
	}
	return pool, added
}

func hasResolvNameserver(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		if i := strings.IndexAny(line, "#;"); i >= 0 {
			line = strings.TrimSpace(line[:i])
		}
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "nameserver" {
			ip := net.ParseIP(fields[1])
			if ip != nil && !ip.IsUnspecified() {
				return true
			}
		}
	}
	return false
}

func splitDNSServers(propIPs []string) (primary, secondary []string) {
	seen := make(map[string]struct{}, len(propIPs)+len(fallbackDNSServers))
	for _, raw := range propIPs {
		raw = strings.TrimSpace(raw)
		ip := net.ParseIP(raw)
		if ip == nil || ip.IsLoopback() || ip.IsUnspecified() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() {
			continue
		}
		hp := net.JoinHostPort(ip.String(), "53")
		if _, exists := seen[hp]; !exists {
			seen[hp] = struct{}{}
			primary = append(primary, hp)
		}
	}
	for _, fb := range fallbackDNSServers {
		if _, exists := seen[fb]; !exists {
			seen[fb] = struct{}{}
			if len(primary) == 0 {
				continue
			}
			secondary = append(secondary, fb)
		}
	}
	if len(primary) == 0 {
		primary = append(primary, fallbackDNSServers...)
	}
	return primary, secondary
}

func candidateDNSServers(propIPs []string) []string {
	primary, secondary := splitDNSServers(propIPs)
	out := make([]string, 0, len(primary)+len(secondary))
	out = append(out, primary...)
	out = append(out, secondary...)
	return out
}

func normalizeDNSNetwork(network string) string {
	if strings.HasPrefix(network, "tcp") {
		return "tcp"
	}
	return "udp"
}

func newFallbackResolver(propFn func() []string) *net.Resolver {
	var seq uint32
	return &net.Resolver{
		PreferGo: true,
		Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			var props []string
			if propFn != nil {
				props = propFn()
			}
			primary, secondary := splitDNSServers(props)
			if len(primary) == 0 {
				return nil, errors.New("no DNS servers available")
			}
			netProto := normalizeDNSNetwork(network)
			start := int(atomic.AddUint32(&seq, 1)-1) % len(primary)
			d := net.Dialer{Timeout: 3 * time.Second}
			var lastErr error
			for i := 0; i < len(primary); i++ {
				target := primary[(start+i)%len(primary)]
				conn, err := d.DialContext(ctx, netProto, target)
				if err == nil {
					return conn, nil
				}
				lastErr = err
			}
			for _, target := range secondary {
				conn, err := d.DialContext(ctx, netProto, target)
				if err == nil {
					return conn, nil
				}
				lastErr = err
			}
			return nil, lastErr
		},
	}
}

var (
	dnsCacheMu      sync.Mutex
	dnsCacheExpires time.Time
	dnsCacheValues  []string
)

func cachedAndroidDNSProps() []string {
	dnsCacheMu.Lock()
	defer dnsCacheMu.Unlock()
	now := time.Now()
	if now.Before(dnsCacheExpires) {
		return append([]string(nil), dnsCacheValues...)
	}
	const getpropBin = "/system/bin/getprop"
	var vals []string
	if st, err := os.Stat(getpropBin); err == nil && !st.IsDir() {
		for _, prop := range []string{"net.dns1", "net.dns2", "net.dns3", "net.dns4"} {
			ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
			out, err := exec.CommandContext(ctx, getpropBin, prop).Output()
			cancel()
			if err == nil {
				if v := strings.TrimSpace(string(out)); v != "" {
					vals = append(vals, v)
				}
			}
		}
	}
	dnsCacheValues = vals
	dnsCacheExpires = now.Add(30 * time.Second)
	return append([]string(nil), vals...)
}
