//go:build race

package importer

// raceEnabled is true when the tests run with the race detector, which makes the code several times slower.
const raceEnabled = true
