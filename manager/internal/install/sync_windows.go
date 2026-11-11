package install

import "os"

// Windows is the local test host, not the production durability target.
func syncTree(_ *os.Root) error { return nil }

func syncParent(_ *os.Root) error { return nil }
