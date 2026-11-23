package webhook

import (
	"crypto/hmac"
	"strconv"
	"time"
)

// Verify is what a receiver does: it checks a signature in constant time and rejects timestamps outside
// MaxSkew of now.
func Verify(secret []byte, timestamp, delivery string, body []byte, signature string, now time.Time) bool {
	ts, err := strconv.ParseInt(timestamp, 10, 64)
	if err != nil {
		return false
	}
	if skew := now.Sub(time.Unix(ts, 0)); skew > MaxSkew || skew < -MaxSkew {
		return false
	}
	return hmac.Equal([]byte(Sign(secret, timestamp, delivery, body)), []byte(signature))
}
