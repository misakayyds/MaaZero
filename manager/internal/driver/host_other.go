//go:build !linux

package driver

import "context"

func prepareHostRuntime(_ context.Context, _ Config) error {
	return nil
}

func startHostWatchdog(_ Config) context.CancelFunc {
	return func() {}
}
