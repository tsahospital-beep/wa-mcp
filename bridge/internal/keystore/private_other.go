//go:build !windows

package keystore

import (
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

const secretOpenFlags = unix.O_NONBLOCK

// checkPrivate refuses a secret file that the group or others may read.
func checkPrivate(f *os.File) error {
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if st.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("%s can be read by other users (mode %04o); run chmod 600 on it", f.Name(), st.Mode().Perm())
	}
	return nil
}
