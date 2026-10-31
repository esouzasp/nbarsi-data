/*
 nBarsi — decifrador do contexto (Cloudflare Worker).

 Baixa data/contexto.enc.json do repositório público, decifra com a chave recebida na URL
 e devolve o contexto em Markdown (padrão) ou JSON. Não guarda a chave nem os dados.

 Endereços:
   /contexto?chave=SUA_CHAVE                 HTML com o texto (padrão; melhor para ChatGPT e Gemini)
   /contexto?chave=SUA_CHAVE&formato=txt     texto puro
   /contexto?chave=SUA_CHAVE&formato=md      Markdown
   /contexto?chave=SUA_CHAVE&formato=json    JSON
   /k/SUA_CHAVE/contexto                     HTML (para quem preferir a chave no caminho)
   /k/SUA_CHAVE/contexto.json                JSON
*/
const SOURCE = "https://raw.githubusercontent.com/esouzasp/nbarsi-data/main/data/contexto.enc.json";

const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const b64url = s => b64(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));

function reply(body, status = 200, type = "text/plain; charset=utf-8") {
  return new Response(body, { status, headers: {
    "content-type": type, "cache-control": "no-store", "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "no-referrer", "access-control-allow-origin": "*" } });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    let key = url.searchParams.get("chave") || url.searchParams.get("key");
    let path = url.pathname;
    const m = path.match(/^\/k\/([A-Za-z0-9_-]{43})(\/.*)?$/);
    if (m) { key = m[1]; path = m[2] || "/contexto"; }
    if (path === "/" || path === "") return reply("nBarsi: use /contexto?chave=SUA_CHAVE");
    if (path === "/versao") return reply("nbarsi-worker 2 (html padrão)");
    if (!/^\/contexto(\.json|\.md)?$/.test(path)) return reply("não encontrado", 404);
    if (!key || !/^[A-Za-z0-9_-]{43}$/.test(key)) return reply("chave ausente ou inválida", 401);

    const res = await fetch(SOURCE, { cf: { cacheTtl: 300, cacheEverything: true } });
    if (!res.ok) return reply("arquivo cifrado indisponível (" + res.status + ")", 502);
    const enc = await res.json();
    let text;
    try {
      const k = await crypto.subtle.importKey("raw", b64url(key), { name: "AES-GCM" }, false, ["decrypt"]);
      const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(enc.iv) }, k, b64(enc.ct));
      text = new TextDecoder().decode(plain);
    } catch (e) {
      return reply("chave incorreta", 401);
    }
    const bundle = JSON.parse(text);
    const fmt = url.searchParams.get("formato") || (path.endsWith(".json") ? "json" : path.endsWith(".md") ? "md" : "html");
    if (fmt === "json") return reply(JSON.stringify(bundle.json, null, 1), 200, "application/json; charset=utf-8");
    if (fmt === "md") return reply(bundle.md, 200, "text/markdown; charset=utf-8");
    if (fmt === "txt") return reply(bundle.md, 200, "text/plain; charset=utf-8");
    // padrão: HTML simples (o formato que as ferramentas de leitura do ChatGPT e do Gemini processam melhor)
    const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const page = `<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="robots" content="noindex, nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>nBarsi — contexto para análise</title></head>
<body><pre style="white-space:pre-wrap;font:14px/1.45 ui-monospace,Menlo,Consolas,monospace;max-width:1100px;margin:16px auto;padding:0 12px">${esc(bundle.md)}</pre></body></html>`;
    return reply(page, 200, "text/html; charset=utf-8");
  }
};
