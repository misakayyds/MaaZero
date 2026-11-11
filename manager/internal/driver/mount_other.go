//go:build !linux

package driver

func newDefaultMounter() Mounter {
	return NewSimulatedMounter()
}
