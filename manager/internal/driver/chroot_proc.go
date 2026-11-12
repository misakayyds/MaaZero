package driver

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// findChrootPIDs returns PIDs in procRoot whose /proc/<pid>/root link resolves
// to rootfsPath (or a subdirectory inside it), excluding PID <= 1 and selfPID.
// Host processes (/proc/<pid>/root -> "/") are never matched.
func findChrootPIDs(procRoot, rootfsPath string, selfPID int, readlink func(string) (string, error)) []int {
	trimmed := strings.TrimSpace(rootfsPath)
	if trimmed == "" || trimmed == "/" || trimmed == "." {
		return nil
	}
	cleanRoot := filepath.Clean(trimmed)
	if cleanRoot == "/" || cleanRoot == "." || cleanRoot == string(filepath.Separator) {
		return nil
	}
	roots := []string{cleanRoot}
	if evalRoot, err := filepath.EvalSymlinks(cleanRoot); err == nil {
		evalRoot = filepath.Clean(evalRoot)
		if evalRoot != "" && evalRoot != "/" && evalRoot != "." && evalRoot != string(filepath.Separator) && evalRoot != cleanRoot {
			roots = append(roots, evalRoot)
		}
	}

	entries, err := os.ReadDir(procRoot)
	if err != nil {
		return nil
	}
	if readlink == nil {
		readlink = os.Readlink
	}
	var pids []int
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid <= 1 || pid == selfPID {
			continue
		}
		target, err := readlink(filepath.Join(procRoot, entry.Name(), "root"))
		if err != nil || target == "" {
			continue
		}
		cleanTarget := filepath.Clean(target)
		for _, r := range roots {
			if cleanTarget == r || strings.HasPrefix(cleanTarget, r+string(filepath.Separator)) {
				pids = append(pids, pid)
				break
			}
		}
	}
	return pids
}
