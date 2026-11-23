package wabridge

import (
	"errors"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/supervise"
)

// ExitFatal is the exit code of Main for errors that a restart does not fix:
// a wrong config, a wrong or missing key, a database of a newer version or a
// store of another account. run-all does not restart a bridge that exits with it.
const ExitFatal = supervise.ExitFatal

type fatalError struct{ err error }

func (e fatalError) Error() string { return e.err.Error() }
func (e fatalError) Unwrap() error { return e.err }

// fatal marks err as one that a restart does not fix.
func fatal(err error) error {
	if err == nil {
		return nil
	}
	return fatalError{err}
}

// IsFatal reports whether err from Run is a configuration or key error that
// a restart does not fix.
func IsFatal(err error) bool {
	var f fatalError
	return errors.As(err, &f)
}
