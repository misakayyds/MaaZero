package install

import (
	"io/fs"
	"os"
)

func syncParent(root *os.Root) error {
	f, err := root.Open(".")
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func syncTree(root *os.Root) error {
	return fs.WalkDir(root.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			return nil
		}
		f, err := root.Open(p)
		if err != nil {
			return err
		}
		defer f.Close()
		return f.Sync()
	})
}
