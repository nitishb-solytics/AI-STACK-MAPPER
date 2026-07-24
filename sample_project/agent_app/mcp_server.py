from mcp.server.fastmcp import FastMCP

mcp = FastMCP("filesystem-server")


@mcp.tool()
def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


@mcp.resource("config://app")
def get_config() -> str:
    return "{}"


if __name__ == "__main__":
    mcp.run()
