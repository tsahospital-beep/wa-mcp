//go:build !windows

package fsutil

import (
	"fmt"
	"os"
	"testing"
)

func sddlOrMode(t *testing.T, path string) string {
	t.Helper()
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return fmt.Sprint(st.Mode().Perm())
}
