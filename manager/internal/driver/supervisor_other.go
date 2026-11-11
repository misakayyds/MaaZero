//go:build !linux

package driver

func newDefaultSupervisor(cfg Config) Supervisor {
	return NewSimulatedSupervisor(cfg)
}
