package fsutil

import (
	"fmt"
	"io/fs"
	"path/filepath"
)

// EFS states reported by EncryptEFS.
const (
	EFSOn          = "on"          // the folder and every file in it are encrypted
	EFSPartial     = "partial"     // some files could not be encrypted (see Failed)
	EFSUnavailable = "unavailable" // EFS cannot be used here (edition, file system or policy)
	EFSNotWindows  = "not_windows" // EFS exists on Windows only
)

// EFSResult describes the EFS state of a folder.
type EFSResult struct {
	State  string
	Reason string   // why EFS is unavailable
	Failed []string // file names that stayed unencrypted
}

// File encryption status values of FileEncryptionStatusW.
const (
	efsEncryptable    = 0
	efsIsEncrypted    = 1
	efsSystemAttr     = 2
	efsRootDir        = 3
	efsSystemDir      = 4
	efsUnknown        = 5
	efsNotSupported   = 6
	efsUserDisallowed = 7
	efsReadOnly       = 8
	efsDirDisallowed  = 9
)

// efsOps are the two Windows calls; tests replace them.
type efsOps struct {
	status  func(path string) (uint32, error)
	encrypt func(path string) error
}

func efsReason(st uint32) string {
	switch st {
	case efsNotSupported:
		return "the file system does not support EFS"
	case efsUserDisallowed:
		return "EFS is disabled for this user or by policy"
	case efsDirDisallowed:
		return "EFS is not allowed for this folder"
	case efsRootDir, efsSystemDir:
		return "EFS is not used for a root or system folder"
	case efsReadOnly:
		return "the folder is read-only"
	case efsSystemAttr:
		return "the folder has the system attribute"
	}
	return fmt.Sprintf("unexpected EFS status %d", st)
}

// applyEFS marks dir for EFS (new files are encrypted) and encrypts the files
// already in it, except the names in skip (open files such as the lock file).
func applyEFS(dir string, skip map[string]bool, ops efsOps) EFSResult {
	st, err := ops.status(dir)
	if err != nil {
		return EFSResult{State: EFSUnavailable, Reason: err.Error()}
	}
	switch st {
	case efsIsEncrypted:
	case efsEncryptable, efsUnknown:
		if err := ops.encrypt(dir); err != nil {
			return EFSResult{State: EFSUnavailable, Reason: err.Error()}
		}
	default:
		return EFSResult{State: EFSUnavailable, Reason: efsReason(st)}
	}
	res := EFSResult{State: EFSOn}
	filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || p == dir || d.Type()&fs.ModeSymlink != 0 {
			return nil
		}
		if skip[d.Name()] {
			return nil
		}
		if s, err := ops.status(p); err == nil && s == efsIsEncrypted {
			return nil
		}
		if err := ops.encrypt(p); err != nil {
			res.Failed = append(res.Failed, d.Name())
		}
		return nil
	})
	if len(res.Failed) > 0 {
		res.State = EFSPartial
	}
	return res
}

// EncryptEFS turns on Windows EFS for dir and the files in it. Files named in
// skip are left as they are. On other systems it reports EFSNotWindows.
func EncryptEFS(dir string, skip ...string) EFSResult {
	ops, ok := platformEFS()
	if !ok {
		return EFSResult{State: EFSNotWindows}
	}
	names := map[string]bool{}
	for _, s := range skip {
		names[s] = true
	}
	return applyEFS(dir, names, ops)
}
