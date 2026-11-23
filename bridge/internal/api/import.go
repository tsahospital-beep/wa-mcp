package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/fsutil"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/importer"
)

const (
	importPath = "/api/v1/import"
	// importFormSlack is room for the form fields around the uploaded file.
	importFormSlack = 1 << 20
	// importFieldMax limits one text field of the upload form.
	importFieldMax = 64 << 10
)

// importMu keeps imports one at a time: they read big files and write many rows.
var importMu sync.Mutex

// bodyLimit is the largest request body: 256 KiB, or the import size limit for an uploaded file.
func (s *Server) bodyLimit(r *http.Request) int64 {
	if r.URL.Path == importPath && strings.HasPrefix(strings.ToLower(r.Header.Get("Content-Type")), "multipart/form-data") {
		return s.Cfg.MaxImportBytes() + importFormSlack
	}
	return maxBody
}

// importRequest holds the settings of one import, from a JSON body or from form fields.
type importRequest struct {
	Path      string            `json:"path"`
	Chat      string            `json:"chat"`
	Me        string            `json:"me"`
	DateOrder string            `json:"date_order"`
	Timezone  string            `json:"timezone"`
	DryRun    bool              `json:"dry_run"`
	Names     map[string]string `json:"names"`
}

func (s *Server) importExport(w http.ResponseWriter, r *http.Request) {
	if !importMu.TryLock() {
		fail(w, http.StatusConflict, "another import is running")
		return
	}
	defer importMu.Unlock()

	// A big file needs more time than the server's default read and write limits.
	rc := http.NewResponseController(w)
	_ = rc.SetReadDeadline(time.Now().Add(15 * time.Minute))
	_ = rc.SetWriteDeadline(time.Now().Add(30 * time.Minute))

	var req importRequest
	var src importer.Source
	var closeInput func()
	if strings.HasPrefix(strings.ToLower(r.Header.Get("Content-Type")), "multipart/form-data") {
		var ok bool
		if src, closeInput, ok = s.readUpload(w, r, &req); !ok {
			return
		}
	} else {
		if !decode(w, r, &req) {
			return
		}
		var ok bool
		if src, closeInput, ok = s.openImportPath(w, req.Path); !ok {
			return
		}
	}
	defer closeInput()

	loc := s.Location
	if loc == nil {
		loc = time.Local
	}
	if tz := strings.TrimSpace(req.Timezone); tz != "" {
		var err error
		if loc, err = time.LoadLocation(tz); err != nil {
			fail(w, http.StatusBadRequest, "timezone: unknown zone "+strconv.Quote(tz)+" (use a name such as Europe/Berlin or UTC)")
			return
		}
	}
	opts := importer.Options{
		Chat: req.Chat, Me: req.Me, Names: req.Names, DateOrder: strings.ToLower(strings.TrimSpace(req.DateOrder)),
		Location: loc, DryRun: req.DryRun, DownloadsDir: s.Cfg.DownloadsDir, Private: s.DB.Encrypted(),
		Omitted: s.Cfg.Import.OmittedPhrases,
		Limits:  importer.Limits{MaxBytes: s.Cfg.MaxImportBytes(), MaxMediaBytes: s.Cfg.MaxDownloadBytes()},
	}
	if p, ok := s.WA.(interface{ PushName() string }); ok {
		opts.PushName = p.PushName()
	}
	rep, err := importer.Run(r.Context(), s.DB, src, opts)
	if err != nil {
		s.importFailed(w, err)
		return
	}
	s.logf("import: %d messages added, %d duplicates, %d files (dry run %v)", rep.MessagesAdded, rep.Duplicates, rep.MediaAdded, rep.DryRun)
	raw, _ := json.Marshal(rep)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	reply(w, http.StatusOK, out)
}

// openImportPath opens a file named by path, which must be inside an allowed folder.
func (s *Server) openImportPath(w http.ResponseWriter, path string) (importer.Source, func(), bool) {
	f, err := importer.OpenInput(path, s.Cfg.ImportDirs(), s.Cfg.Sending.DeniedPaths, s.Cfg.Sending.ForeignMedia)
	if err != nil {
		var ie *importer.InputError
		switch {
		case errors.As(err, &ie) && ie.Forbidden:
			fail(w, http.StatusForbidden, err.Error())
		case errors.As(err, &ie) && ie.Missing:
			fail(w, http.StatusNotFound, err.Error())
		default:
			fail(w, http.StatusBadRequest, err.Error())
		}
		return importer.Source{}, nil, false
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		fail(w, http.StatusInternalServerError, err.Error())
		return importer.Source{}, nil, false
	}
	return importer.Source{R: f, Size: st.Size(), Name: filepath.Base(path)}, func() { f.Close() }, true
}

// readUpload stores the file part of a multipart form in a private temporary file and reads the
// other parts as settings.
func (s *Server) readUpload(w http.ResponseWriter, r *http.Request, req *importRequest) (importer.Source, func(), bool) {
	mr, err := r.MultipartReader()
	if err != nil {
		fail(w, http.StatusBadRequest, "multipart form expected: "+err.Error())
		return importer.Source{}, nil, false
	}
	var tmp *os.File
	cleanup := func() {
		if tmp != nil {
			tmp.Close()
			fsutil.Wipe(tmp.Name()) // the upload holds chat text in plain form
		}
	}
	tooBig := func(err error) bool {
		var mb *http.MaxBytesError
		if errors.As(err, &mb) {
			fail(w, http.StatusRequestEntityTooLarge, "the file is larger than import.max_mb")
			return true
		}
		return false
	}
	name := ""
	for {
		part, err := mr.NextPart()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			cleanup()
			if !tooBig(err) {
				fail(w, http.StatusBadRequest, "bad multipart body: "+err.Error())
			}
			return importer.Source{}, nil, false
		}
		field := part.FormName()
		if field == "file" {
			if tmp != nil {
				cleanup()
				fail(w, http.StatusBadRequest, "send one file")
				return importer.Source{}, nil, false
			}
			if tmp, err = fsutil.CreateTemp(filepath.Join(s.Cfg.StoreDir, "import-tmp"), "upload-*"); err != nil {
				fail(w, http.StatusInternalServerError, "cannot create a temporary file")
				return importer.Source{}, nil, false
			}
			name = common.BaseName(part.FileName())
			if _, err := io.Copy(tmp, part); err != nil {
				cleanup()
				if !tooBig(err) {
					fail(w, http.StatusBadRequest, "upload failed: "+err.Error())
				}
				return importer.Source{}, nil, false
			}
			continue
		}
		val, err := io.ReadAll(io.LimitReader(part, importFieldMax+1))
		if err != nil || len(val) > importFieldMax {
			cleanup()
			fail(w, http.StatusBadRequest, "form field "+strconv.Quote(field)+" is unreadable or too long")
			return importer.Source{}, nil, false
		}
		text := string(val)
		switch field {
		case "chat":
			req.Chat = text
		case "me":
			req.Me = text
		case "date_order":
			req.DateOrder = text
		case "timezone":
			req.Timezone = text
		case "dry_run":
			req.DryRun, _ = strconv.ParseBool(strings.TrimSpace(text))
		case "path":
			req.Path = text
		case "names":
			if strings.TrimSpace(text) != "" {
				if err := json.Unmarshal(val, &req.Names); err != nil {
					cleanup()
					fail(w, http.StatusBadRequest, "names: want a JSON object that maps a sender name to a JID or phone number")
					return importer.Source{}, nil, false
				}
			}
		}
	}
	if tmp == nil {
		fail(w, http.StatusBadRequest, "no file part: send the export as the form field \"file\"")
		return importer.Source{}, nil, false
	}
	if req.Path != "" {
		cleanup()
		fail(w, http.StatusBadRequest, "send either a file or a path, not both")
		return importer.Source{}, nil, false
	}
	st, err := tmp.Stat()
	if err != nil {
		cleanup()
		fail(w, http.StatusInternalServerError, err.Error())
		return importer.Source{}, nil, false
	}
	return importer.Source{R: tmp, Size: st.Size(), Name: name}, cleanup, true
}

// importFailed maps an import error to a status and an answer that says what to change.
func (s *Server) importFailed(w http.ResponseWriter, err error) {
	var ce *importer.ChatError
	var me *importer.MeError
	var fe *importer.RequestError
	switch {
	case errors.As(err, &ce):
		status := http.StatusNotFound
		if ce.Ambiguous {
			status = http.StatusConflict
		}
		reply(w, status, map[string]any{"error": ce.Msg, "candidates": ce.Candidates})
	case errors.As(err, &me):
		reply(w, http.StatusBadRequest, map[string]any{"error": me.Msg, "senders": me.Senders})
	case errors.Is(err, importer.ErrTooLarge):
		fail(w, http.StatusRequestEntityTooLarge, err.Error())
	case errors.As(err, &fe):
		fail(w, http.StatusBadRequest, err.Error())
	default:
		s.logf("import: %v", err)
		fail(w, http.StatusInternalServerError, err.Error())
	}
}
