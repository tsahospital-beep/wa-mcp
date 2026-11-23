// Command wa-bridge connects one WhatsApp account and serves the local API
// used by the wa-mcp Python package. The code lives in pkg/wabridge, which
// other Go programs can use to embed the bridge.
package main

import (
	"os"

	"github.com/tsahospital-beep/wa-mcp/bridge/pkg/wabridge"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "0.1.1"

func main() { os.Exit(wabridge.Main(os.Args[1:], wabridge.Options{Version: version})) }
