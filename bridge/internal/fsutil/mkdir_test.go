package fsutil

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMkdirPrivateLeavesExistingFoldersAlone(t *testing.T) {
	user := filepath.Join(t.TempDir(), "my-downloads")
	os.MkdirAll(user, 0o755)
	before := sddlOrMode(t, user)
	deep := filepath.Join(user, "chat", "sub")
	if err := MkdirPrivate(deep); err != nil {
		t.Fatal(err)
	}
	if sddlOrMode(t, user) != before {
		t.Fatal("an existing folder of the user was changed")
	}
	checkPrivate(t, filepath.Join(user, "chat"), true)
	checkPrivate(t, deep, true)

	owned := filepath.Join(t.TempDir(), "owned")
	os.MkdirAll(owned, 0o755)
	if err := MkdirOwned(owned); err != nil {
		t.Fatal(err)
	}
	checkPrivate(t, owned, true)
}
