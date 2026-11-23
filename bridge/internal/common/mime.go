package common

import "strings"

const binaryMIME = "application/octet-stream"

type mediaFormat struct {
	ext, mime      string
	send, download bool
}

// The download flag selects the preferred suffix for a received MIME type.
var formats = []mediaFormat{
	{".jpg", "image/jpeg", true, true},
	{".jpeg", "image/jpeg", true, false},
	{".png", "image/png", true, true},
	{".gif", "image/gif", true, true},
	{".heic", "image/heic", true, true},
	{".webp", "image/webp", true, true},
	{".bmp", "image/bmp", false, false},
	{".tif", "image/tiff", false, false},
	{".tiff", "image/tiff", false, false},
	{".mp4", "video/mp4", true, true},
	{".3gp", "video/3gpp", true, true},
	{".mov", "video/quicktime", true, true},
	{".mkv", "video/x-matroska", true, false},
	{".avi", "video/x-msvideo", true, false},
	{".webm", "video/webm", true, true},
	{".m4v", "video/mp4", false, false},
	{".opus", "audio/ogg", true, false},
	{".ogg", "audio/ogg", true, true},
	{".oga", "audio/ogg", true, false},
	{".mp3", "audio/mpeg", true, true},
	{".m4a", "audio/mp4", true, true},
	{".aac", "audio/aac", true, true},
	{".wav", "audio/wav", true, true},
	{".amr", "audio/amr", true, true},
	{".pdf", "application/pdf", true, true},
	{".txt", "text/plain", true, true},
	{".csv", "text/csv", true, false},
	{".vcf", "text/vcard", false, false},
	{".zip", "application/zip", true, true},
	{".doc", "application/msword", true, false},
	{".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", true, false},
	{".xls", "application/vnd.ms-excel", true, false},
	{".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", true, false},
	{".ppt", "application/vnd.ms-powerpoint", true, false},
	{".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", true, false},
}

func MediaMIME(ext string, sending bool) string {
	for _, f := range formats {
		if f.ext == ext && (!sending || f.send) {
			if sending && (ext == ".ogg" || ext == ".opus") {
				return f.mime + "; codecs=opus"
			}
			return f.mime
		}
	}
	return binaryMIME
}

func MediaExtension(mime, fallback string) string {
	base, _, _ := strings.Cut(mime, ";")
	base = strings.ToLower(strings.TrimSpace(base))
	for _, f := range formats {
		if f.download && f.mime == base {
			return f.ext
		}
	}
	return fallback
}
