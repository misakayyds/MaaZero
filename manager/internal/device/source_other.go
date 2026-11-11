//go:build !linux

package device

// newSystemSource reports that this host cannot probe an Android device. The
// project only declares Android 11+ arm64 support, so a foreign host is an
// explicit unsupported error rather than a guess.
func newSystemSource() (ProbeSource, error) { return nil, ErrUnsupportedHost }
