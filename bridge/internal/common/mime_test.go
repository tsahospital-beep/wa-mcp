package common

import "testing"

func TestMediaFormatByOperation(t *testing.T) {
	for _, c := range []struct{ ext, imported, sent, downloaded string }{
		{".opus", "audio/ogg", "audio/ogg; codecs=opus", ".ogg"},
		{".oga", "audio/ogg", "audio/ogg", ".ogg"},
		{".jpeg", "image/jpeg", "image/jpeg", ".jpg"},
		{".m4v", "video/mp4", binaryMIME, ".mp4"},
		{".bmp", "image/bmp", binaryMIME, ".bin"},
		{".unknown", binaryMIME, binaryMIME, ".bin"},
	} {
		if MediaMIME(c.ext, false) != c.imported || MediaMIME(c.ext, true) != c.sent ||
			MediaExtension(c.imported+"; extra", ".bin") != c.downloaded {
			t.Errorf("format %s", c.ext)
		}
	}
}
