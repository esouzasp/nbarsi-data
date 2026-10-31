/*
 nBarsi — decifrador do contexto (Cloudflare Worker).

 Baixa data/contexto.enc.json do repositório público, decifra com a chave recebida na URL
 e devolve o contexto em Markdown (padrão) ou JSON. Não guarda a chave nem os dados.

 Endereços:
   /contexto?chave=SUA_CHAVE                 Markdown
   /contexto?chave=SUA_CHAVE&formato=json    JSON
   /k/SUA_CHAVE/contexto                     Markdown (para quem preferir a chave no caminho)
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
    const wantJson = url.searchParams.get("formato") === "json" || path.endsWith(".json");
    if (wantJson) return reply(JSON.stringify(bundle.json, null, 1), 200, "application/json; charset=utf-8");
    return reply(bundle.md, 200, "text/markdown; charset=utf-8");
  }
};
