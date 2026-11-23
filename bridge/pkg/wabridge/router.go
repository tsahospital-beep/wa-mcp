package wabridge

import (
	"context"
	"errors"
	"log"
	"net/http"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/api"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// router collects the routes added through Router.
type router struct{ routes []api.Route }

func (r *router) add(pattern string, h http.Handler, noToken bool) error {
	if h == nil {
		return errors.New("route " + pattern + ": handler is nil")
	}
	next := append(append([]api.Route{}, r.routes...), api.Route{Pattern: pattern, Handler: h, NoToken: noToken})
	if err := api.CheckRoutes(next); err != nil {
		return err
	}
	r.routes = next
	return nil
}

func (r *router) Handle(pattern string, h http.Handler) error { return r.add(pattern, h, false) }

func (r *router) HandleWithoutToken(pattern string, h http.Handler) error {
	return r.add(pattern, h, true)
}

// collectRoutes runs the Routes option.
func collectRoutes(fn func(Router, Deps) error, d Deps) ([]api.Route, error) {
	if fn == nil {
		return nil, nil
	}
	r := &router{}
	if err := fn(r, d); err != nil {
		return nil, err
	}
	return r.routes, nil
}

// RoutesHandler serves only the routes that routes registers, with the same
// Host, token and body size checks as the bridge API on listen (host:port).
// It is meant for tests of embedder routes without a WhatsApp connection.
func RoutesHandler(listen, token string, d Deps, routes func(Router, Deps) error) (http.Handler, error) {
	list, err := collectRoutes(routes, d)
	if err != nil {
		return nil, err
	}
	return api.ExtraOnly(listen, token, list)
}

// observer turns the store notifications into the hook calls.
func observer(opts Options, logger *log.Logger) *store.Observer {
	if opts.OnMessageStored == nil && opts.OnChatStored == nil {
		return nil
	}
	guard := func(what string, fn func()) {
		defer func() {
			if r := recover(); r != nil {
				logger.Printf("error: %s hook: %v", what, r)
			}
		}()
		fn()
	}
	o := &store.Observer{}
	if opts.OnMessageStored != nil {
		o.Message = func(ctx context.Context, origin string, m *store.Message, chat *store.Conversation) {
			title := ""
			if chat != nil {
				title = chat.Title
			}
			sm := toStored(origin, m, title)
			guard("OnMessageStored", func() { opts.OnMessageStored(ctx, sm) })
		}
	}
	if opts.OnChatStored != nil {
		o.Conversation = func(ctx context.Context, c *store.Conversation) {
			sc := toChat(c)
			guard("OnChatStored", func() { opts.OnChatStored(ctx, sc) })
		}
	}
	return o
}
