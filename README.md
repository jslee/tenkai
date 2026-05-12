#

## Naver Search MCP
<https://github.com/isnow890/naver-search-mcp/blob/main/RELEASE_NOTES.md>

API 사용을 위한 등록페이지:
<https://developers.naver.com/apps/#/myapps/EhvcspVdFnDVgN3E3aBe/overview>

### MCP 등록
''' json
{
  "mcpServers": {
    "naver-search": {
      "type": "stdio",
      "command": "cmd",
      "args": [
        "/c",
        "node",
        "D:\\Projects\\tenkai\\naver-search-mcp\\dist\\src\\index.js"
      ],
      "cwd": "D:\\Projects\\tenkai\\naver-search-mcp",
      "env": {
        "NAVER_CLIENT_ID": "...",
        "NAVER_CLIENT_SECRET": "..."
      }
    }
  }
}
'''

### conda 환경에서 사용
``` bash
> conda env config vars set NAVER_CLIENT_ID=value
> conda env config vars set NAVER_CLIENT_SECRET=value

```
