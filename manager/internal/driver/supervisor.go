package driver

import (
	"context"
	"errors"
	"net/http"
	"time"
)

// Supervisor oversees background services (WebUI, cron) inside the chroot.
type Supervisor interface {
	Start(ctx context.Context, cfg Config, bundleDir string) error
	Stop(ctx context.Context) error
	WaitReady(ctx context.Context, timeout time.Duration) error
	Running() bool
}

// waitReadyProbe polls probeURL using HTTP GET until a response is received or ctx/timeout expires.
func waitReadyProbe(ctx context.Context, probeURL string, timeout time.Duration, isRunning func() bool) error {
	client := &http.Client{Timeout: 500 * time.Millisecond}
	deadline := time.Now().Add(timeout)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if time.Now().After(deadline) {
			return errors.New("readiness probe timed out waiting for " + probeURL)
		}
		if isRunning != nil && !isRunning() {
			return errors.New("supervised process exited before readiness probe succeeded")
		}

		resp, err := client.Get(probeURL)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
}
