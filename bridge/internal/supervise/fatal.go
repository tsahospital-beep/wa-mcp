package supervise

// ExitFatal is the exit code of a bridge that cannot start because of its
// configuration or its keys (EX_CONFIG of sysexits.h). Starting it again
// would fail the same way, so the supervisor does not.
const ExitFatal = 78
