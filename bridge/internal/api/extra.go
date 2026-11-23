package api

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
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

// isReserved reports whether a request path belongs to the bridge.
func isReserved(path string) bool {
	return path == "/" || path == HealthPath || path == "/api/v1" || strings.HasPrefix(path, "/api/v1/")
}

// overlapsBridge compares pattern segments with the reserved namespace. A
// trailing slash and a multi-segment wildcard both cover the remaining path.
func overlapsBridge(pattern string) bool {
	parts := strings.Split(strings.TrimPrefix(pattern, "/"), "/")
	matches := func(prefix []string, subtree bool) bool {
		for i, part := range parts {
			if part == "{$}" {
				return i == len(prefix) || (subtree && i >= len(prefix))
			}
			if part == "" || strings.HasPrefix(part, "{") && strings.HasSuffix(part, "...}") {
				return true
			}
			if i >= len(prefix) {
				return subtree
			}
			if !strings.HasPrefix(part, "{") {
				literal, err := url.PathUnescape(part)
				if err != nil || literal != prefix[i] {
					return false
				}
			}
		}
		return len(parts) == len(prefix)
	}
	return matches([]string{"api"}, false) || matches([]string{"api", "v1"}, true) || matches([]string{"health"}, false) || matches(nil, false)
}

// CheckRoutes reports a pattern that is malformed, registered twice, or able
// to match a path of the bridge (/, /health and everything under /api/v1).
// The reserved namespace applies to every method. Wildcards such as "/{p...}" or "/api/{v}/status" are refused too.
func CheckRoutes(routes []Route) error {
	all := http.NewServeMux()
	for _, rt := range routes {
		if rt.Handler == nil {
			return fmt.Errorf("route %q: handler is nil", rt.Pattern)
		}
		host := ""
		path := rt.Pattern
		if i := strings.IndexAny(path, " \t"); i >= 0 {
			path = strings.TrimSpace(path[i+1:])
		}
		if i := strings.IndexByte(path, '/'); i > 0 {
			host, path = path[:i], path[i:]
		}
		if strings.Contains(host, ":") {
			// ServeMux compares the host without its port, so such a pattern would never match.
			return fmt.Errorf("route %q: a host in a pattern must not have a port", rt.Pattern)
		}
		if !strings.HasPrefix(path, "/") || isReserved(path) {
			return fmt.Errorf("route %q: the path must start with / and must not be /, %s or under /api/v1", rt.Pattern, HealthPath)
		}
		if overlapsBridge(path) {
			return fmt.Errorf("route %q matches a path reserved by the bridge", rt.Pattern)
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
		if s.serveOpen(mux, open, w, r) {
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

// Use one router for both policies so a wildcard cannot mask a more
// specific protected pattern. ServeMux also supplies the wildcard values.
func (s *Server) mountExtra(mux *http.ServeMux) map[string]bool {
	open := make(map[string]bool)
	for _, rt := range s.Extra {
		mux.Handle(rt.Pattern, rt.Handler)
		if rt.NoToken {
			open[rt.Pattern] = true
		}
	}
	return open
}

func (s *Server) serveOpen(mux *http.ServeMux, open map[string]bool, w http.ResponseWriter, r *http.Request) bool {
	if len(open) == 0 || isReserved(r.URL.Path) {
		return false
	}
	_, pattern := mux.Handler(r)
	if !open[pattern] {
		return false
	}
	if !loopbackPeer(r) && !s.authorized(r) {
		return false
	}
	if !browserRequestAllowed(r) {
		fail(w, http.StatusForbidden, "cross-origin request forbidden")
		return true
	}
	if r.Body != nil {
		r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	}
	s.inflight.Add(1)
	defer s.inflight.Done()
	mux.ServeHTTP(w, r)
	return true
}

// Browser pages can post to a loopback service without reading the response.
func browserRequestAllowed(r *http.Request) bool {
	if strings.EqualFold(r.Header.Get("Sec-Fetch-Site"), "cross-site") {
		return false
	}
	origins := r.Header.Values("Origin")
	if len(origins) == 0 {
		return true
	}
	if len(origins) != 1 {
		return false
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return strings.EqualFold(origins[0], scheme+"://"+r.Host)
}
