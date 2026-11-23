//go:build !windows

package fsutil

func platformEFS() (efsOps, bool) { return efsOps{}, false }
