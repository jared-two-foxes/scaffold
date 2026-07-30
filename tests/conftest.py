import sys
import types

render_stub = types.ModuleType("ticket_pipeline.lib.render")
render_stub.render_markdown = lambda _text: None
render_stub.print_line = lambda *args, **kwargs: None
sys.modules.setdefault("ticket_pipeline.lib.render", render_stub)
