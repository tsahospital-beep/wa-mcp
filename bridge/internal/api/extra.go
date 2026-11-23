package api

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
)

// Route is an extra handler added by an embedding program (pkg/wabridge).
// Routes with NoToken skip the bearer token check only for callers on a
// loopback address; other callers need the token. The Host check, the body
// size limit and the shutdown wait always apply.
type Route struct {
	Pattern string
	Handler http.Handler
	NoToken bool
}

// reservedPaths are sample paths of the bridge; no extra pattern may match
// any of them, with any method, also through wildcards.
var reservedPaths = []string{
	"/", HealthPath, "/api", "/api/", "/api/v1", "/api/v1/", "/api/v1/status", "/api/v1/messages",
	"/api/v1/send", "/api/v1/schedule/1", "/api/v1/openapi.yaml", "/api/v1/x/y/z",
}

var probeMethods = []string{"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

// isReserved reports whether a request path belongs to the bridge.
func isReserved(path string) bool {
	return path == "/" || path == HealthPath || path == "/api/v1" || strings.HasPrefix(path, "/api/v1/")
}

// CheckRoutes reports a pattern that is malformed, registered twice, or able
// to match a path of the bridge (/, /health and everything under /api/v1).
// Each pattern is tried against sample paths of the bridge with every
// method, so wildcards such as "/{p...}" or "/api/{v}/status" are refused too.
func CheckRoutes(routes []Route) error {
	all := http.NewServeMux()
	for _, rt := range routes {
		if rt.Handler == nil {
			return fmt.Errorf("route %q: handler is nil", rt.Pattern)
		}
		one := http.NewServeMux()
		if err := tryHandle(one, rt.Pattern, rt.Handler); err != nil {
			return fmt.Errorf("route %q: %w", rt.Pattern, err)
		}
		host := "127.0.0.1:1"
		path := rt.Pattern
		if i := strings.IndexByte(path, ' '); i >= 0 {
			path = strings.TrimSpace(path[i+1:])
		}
		if i := strings.IndexByte(path, '/'); i > 0 {
			host, path = path[:i], path[i:]
		}
		if strings.Contains(host, ":") && host != "127.0.0.1:1" {
			// ServeMux compares the host without its port, so such a pattern would never match.
			return fmt.Errorf("route %q: a host in a pattern must not have a port", rt.Pattern)
		}
		if !strings.HasPrefix(path, "/") || isReserved(path) {
			return fmt.Errorf("route %q: the path must start with / and must not be /, %s or under /api/v1", rt.Pattern, HealthPath)
		}
		for _, p := range reservedPaths {
			for _, m := range probeMethods {
				req, err := http.NewRequest(m, "http://"+host+p, nil)
				if err != nil {
					return fmt.Errorf("route %q: %w", rt.Pattern, err)
				}
				if _, pattern := one.Handler(req); pattern != "" {
					return fmt.Errorf("route %q matches %s %s, which belongs to the bridge (/, %s and everything under /api/v1)",
						rt.Pattern, m, p, HealthPath)
				}
			}
		}
		if err := tryHandle(all, rt.Pattern, rt.Handler); err != nil {
			return fmt.Errorf("route %q: %w", rt.Pattern, err)
		}
	}
	return nil
}

// loopbackPeer reports whether the connection comes from this machine. The
// Host header cannot tell, because a remote client chooses it.
func loopbackPeer(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func tryHandle(mux *http.ServeMux, pattern string, h http.Handler) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = errors.New(fmt.Sprint(r))
		}
	}()
	mux.Handle(pattern, h)
	return nil
}

// CheckExtra builds the handler once to find extra routes that conflict with
// the bridge's own routes, so a conflict is an error before serving starts.
func (s *Server) CheckExtra() (err error) {
	if err := CheckRoutes(s.Extra); err != nil {
		return err
	}
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("extra routes: %v", r)
		}
	}()
	s.Handler("127.0.0.1:1")
	return nil
}

// ExtraOnly serves only the given routes with the same Host, token and body
// checks as Handler; every other path answers 404. It lets embedders test
// their routes without a WhatsApp connection.
func ExtraOnly(addr, token string, routes []Route) (http.Handler, error) {
	if err := CheckRoutes(routes); err != nil {
		return nil, err
	}
	s := &Server{Token: token, Extra: routes}
	hosts := allowedHosts(addr)
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { fail(w, http.StatusNotFound, "not found") })
	open := s.mountExtra(mux)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !hosts[strings.ToLower(r.Host)] {
			fail(w, http.StatusForbidden, "host not allowed")
			return
		}
		if s.serveOpen(open, w, r) {
			return
		}
		if !s.authorized(r) {
			fail(w, http.StatusUnauthorized, "missing or wrong bearer token")
			return
		}
		if r.Body != nil {
			r.Body = http.MaxBytesReader(w, r.Body, maxBody)
		}
		mux.ServeHTTP(w, r)
	}), nil
}

// mountExtra adds the token routes to mux and returns a mux with the routes
// that need no token (nil when there are none). Call CheckRoutes first.
func (s *Server) mountExtra(mux *http.ServeMux) *http.ServeMux {
	var open *http.ServeMux
	for _, rt := range s.Extra {
		if rt.NoToken {
			if open == nil {
				open = http.NewServeMux()
			}
			open.Handle(rt.Pattern, rt.Handler)
			continue
		}
		mux.Handle(rt.Pattern, rt.Handler)
	}
	return open
}

// serveOpen serves r when it matches a route without a token and comes from
// a loopback address, and reports whether it did. Paths of the bridge are
// never served here, whatever the patterns are.
func (s *Server) serveOpen(open *http.ServeMux, w http.ResponseWriter, r *http.Request) bool {
	if open == nil || isReserved(r.URL.Path) || !loopbackPeer(r) {
		return false
	}
	h, pattern := open.Handler(r)
	if pattern == "" {
		return false
	}
	if r.Body != nil {
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	}
	s.inflight.Add(1)
	defer s.inflight.Done()
	h.ServeHTTP(w, r)
	return true
}
