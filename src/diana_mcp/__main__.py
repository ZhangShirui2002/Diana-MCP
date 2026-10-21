"""python -m diana_mcp               run the MCP server (stdio)
python -m diana_mcp watch [...]     follow the user's DianaIE edits (see watch.py)
python -m diana_mcp autostart status|enable|disable
python -m diana_mcp knowledge init|status|validate|commit -m "..."
"""
import sys


def run() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "watch":
        from diana_mcp.watch import main
        return main(sys.argv[2:])
    if command == "autostart":
        from diana_mcp.autostart import main
        return main(sys.argv[2:])
    if command == "knowledge":
        from diana_mcp.knowledge_cli import main
        return main(sys.argv[2:])
    from diana_mcp.server import main
    main()
    return 0


sys.exit(run())
