//go:build !linux

package driver

import "context"

func newDefaultSupervisor(cfg Config) Supervisor {
	return NewSimulatedSupervisor(cfg)
}

func reapChrootProcesses(context.Context, string) {}
