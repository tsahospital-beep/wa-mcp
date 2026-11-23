//go:build !windows

package keystore

import (
	"fmt"
	"os"
)

// checkPrivate refuses a secret file that the group or others may read.
func checkPrivate(path string) error {
	st, err := os.Stat(path)
	if err != nil {
		return err
	}
	if st.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("%s can be read by other users (mode %04o); run chmod 600 on it", path, st.Mode().Perm())
	}
	return nil
}
