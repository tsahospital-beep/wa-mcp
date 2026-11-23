package store

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// The bridge writes MinReaderSchema (and only it) to meta.min_reader_schema;
// the Python reader must know at least that schema.
func TestMinReaderSchemaHasOneSource(t *testing.T) {
	for _, m := range migrations {
		if strings.Contains(m, "min_reader_schema") {
			t.Fatal("a migration writes min_reader_schema; migrate() writes the Go constant")
		}
	}
	db := openTemp(t)
	if v, _ := db.Meta(ctx, "min_reader_schema"); v != strconv.Itoa(MinReaderSchema) {
		t.Fatalf("meta %q, constant %d", v, MinReaderSchema)
	}
	db.SetMeta(ctx, "min_reader_schema", "9")
	db.setMinReader(ctx)
	if v, _ := db.Meta(ctx, "min_reader_schema"); v != "9" {
		t.Fatal("a higher value was lowered")
	}
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "sdk", "python", "wa_mcp", "db.py"))
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?m)^READER_SCHEMA = (\d+)`).FindSubmatch(src)
	if m == nil {
		t.Fatal("READER_SCHEMA not found in db.py")
	}
	if n, _ := strconv.Atoi(string(m[1])); n != MinReaderSchema {
		t.Fatalf("Python READER_SCHEMA %d, Go MinReaderSchema %d", n, MinReaderSchema)
	}
}
