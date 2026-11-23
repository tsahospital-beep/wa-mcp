package wabridge

import "io"

// run is the command line with default options, as the tests moved from cmd/wa-bridge call it.
func run(args []string, stdout, stderr io.Writer) int {
	return Main(args, Options{Stdout: stdout, Stderr: stderr, Version: "test"})
}
