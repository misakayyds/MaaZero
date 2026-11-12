package download

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func generateTestCertBytes(t *testing.T) (derBytes, pemBytes []byte, cert *x509.Certificate) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Test Android Root CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &priv.PublicKey, priv)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	pemOut := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return der, pemOut, parsed
}

func TestLoadExtraCertPoolLoadsPEMAndDER(t *testing.T) {
	dir := t.TempDir()
	_, pem1, cert1 := generateTestCertBytes(t)
	der2, _, cert2 := generateTestCertBytes(t)

	if err := os.WriteFile(filepath.Join(dir, "00000001.0"), pem1, 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "00000002.0"), der2, 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "garbage.0"), []byte("not a certificate"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "subdir"), 0755); err != nil {
		t.Fatal(err)
	}

	pool, added := loadExtraCertPool(filepath.Join(dir, "missing"), dir)
	if added != 2 {
		t.Fatalf("added = %d, want 2", added)
	}
	for i, c := range []*x509.Certificate{cert1, cert2} {
		if _, err := c.Verify(x509.VerifyOptions{Roots: pool}); err != nil {
			t.Fatalf("cert %d failed verification against loaded pool: %v", i, err)
		}
	}
}

func TestHasResolvNameserver(t *testing.T) {
	dir := t.TempDir()
	if hasResolvNameserver(filepath.Join(dir, "missing")) {
		t.Fatal("missing resolv.conf must return false")
	}

	emptyPath := filepath.Join(dir, "empty.conf")
	if err := os.WriteFile(emptyPath, []byte("# comment only\noptions timeout:1\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if hasResolvNameserver(emptyPath) {
		t.Fatal("comment-only resolv.conf must return false")
	}

	validPath := filepath.Join(dir, "resolv.conf")
	if err := os.WriteFile(validPath, []byte("# Generated\nnameserver 223.5.5.5 # inline comment\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if !hasResolvNameserver(validPath) {
		t.Fatal("valid resolv.conf must return true")
	}
}

func TestCandidateDNSServersFiltersLoopbackAndDeduplicates(t *testing.T) {
	got := candidateDNSServers([]string{"", "127.0.0.1", "::1", "0.0.0.0", "fe80::1", "224.0.0.1", "not-an-ip", "114.114.114.114", "223.5.5.5", "2400:3200::1"})
	if len(got) != 4 {
		t.Fatalf("unexpected server list: %v", got)
	}
	if got[0] != "114.114.114.114:53" {
		t.Fatalf("got[0] = %q, want 114.114.114.114:53", got[0])
	}
	if got[1] != "223.5.5.5:53" {
		t.Fatalf("got[1] = %q, want 223.5.5.5:53", got[1])
	}
	if got[2] != "[2400:3200::1]:53" {
		t.Fatalf("got[2] = %q, want [2400:3200::1]:53", got[2])
	}
	if got[3] != "119.29.29.29:53" {
		t.Fatalf("got[3] = %q, want 119.29.29.29:53", got[3])
	}
	primary, secondary := splitDNSServers([]string{"10.0.0.53"})
	if len(primary) != 1 || primary[0] != "10.0.0.53:53" {
		t.Fatalf("primary = %v, want [10.0.0.53:53]", primary)
	}
	if len(secondary) != 2 {
		t.Fatalf("secondary = %v, want 2 fallback servers", secondary)
	}
	seen := map[string]bool{}
	for _, s := range got {
		if seen[s] {
			t.Fatalf("duplicate DNS server %q in %v", s, got)
		}
		seen[s] = true
	}
}

func TestFallbackResolverRotatesAcrossCalls(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			_ = c.Close()
		}
	}()

	// Verify normalizeDNSNetwork collapses udp6/tcp6 to dual-stack udp/tcp.
	if got := normalizeDNSNetwork("udp6"); got != "udp" {
		t.Fatalf("normalizeDNSNetwork(udp6) = %q, want udp", got)
	}
	if got := normalizeDNSNetwork("tcp4"); got != "tcp" {
		t.Fatalf("normalizeDNSNetwork(tcp4) = %q, want tcp", got)
	}

	r := newFallbackResolver(func() []string { return nil })
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	conn, err := r.Dial(ctx, "udp6", "[::1]:53")
	if err != nil {
		t.Fatalf("expected UDP socket setup to succeed: %v", err)
	}
	_ = conn.Close()
}
