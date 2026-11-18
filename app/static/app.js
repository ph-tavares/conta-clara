"use strict";
const $ = (id) => document.getElementById(id);
const CHAVE = "contaclara.codigo";
let codigo = null;
const brl = (v) => v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

function lembrar(c) {
  codigo = c;
  try { localStorage.setItem(CHAVE, c); } catch (_) { /* navegador sem armazenamento: vale so nesta aba */ }
  $("status-casa").textContent = c === "demonstracao"
    ? "Casa de demonstração aberta (leituras fictícias)."
    : "Casa aberta. Guarde o código para usar em outro aparelho: " + c;
  carregar();
}

async function api(caminho, opcoes = {}) {
  const headers = { "Content-Type": "application/json", ...(opcoes.headers || {}) };
  if (codigo) headers["X-Codigo-Casa"] = codigo;
  const r = await fetch(caminho, { ...opcoes, headers });
  const corpo = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof corpo.detail === "string" ? corpo.detail : "verifique os dados informados");
  return corpo;
}

async function carregar() {
  if (!codigo) return;
  try {
    const { leituras, resumo } = await api("/api/leituras");
    $("leituras").replaceChildren(...leituras.slice().reverse().map((l) => {
      const tr = document.createElement("tr");
      for (const v of [new Date(l.data + "T12:00").toLocaleDateString("pt-BR"), l.kwh]) {
        const td = document.createElement("td"); td.textContent = v; tr.append(td);
      }
      return tr;
    }));
    $("resumo").innerHTML = resumo
      ? `<p>Você gasta em média <span class="destaque">${resumo.consumo_diario_kwh.toLocaleString("pt-BR")} kWh por dia</span>.</p>
         <p>Em 30 dias: <strong>${resumo.projecao_30d_kwh.toLocaleString("pt-BR")} kWh</strong>,
         conta estimada de <span class="destaque">${brl(resumo.estimativa_30d_reais)}</span>.</p>`
      : "<p>Anote ao menos 2 leituras em dias diferentes.</p>";
  } catch (e) {
    $("status-casa").textContent = "Não foi possível abrir a casa: " + e.message;
  }
}

$("nova").onclick = async () => { codigo = null; lembrar((await api("/api/casas", { method: "POST" })).codigo); };
$("demo").onclick = () => lembrar("demonstracao");
$("abrir").onclick = () => { const c = $("codigo").value.trim(); if (c) lembrar(c); };

$("form-leitura").onsubmit = async (ev) => {
  ev.preventDefault();
  const msg = $("msg-leitura");
  if (!codigo) { msg.textContent = "Primeiro abra ou crie uma casa (passo 1)."; return; }
  try {
    await api("/api/leituras", { method: "POST", body: JSON.stringify({ data: $("data").value, kwh: Number($("kwh").value) }) });
    msg.textContent = "Leitura salva.";
    $("kwh").value = "";
    carregar();
  } catch (e) { msg.textContent = "Não salvou: " + e.message; }
};

$("form-conferir").onsubmit = async (ev) => {
  ev.preventDefault();
  const out = $("resultado");
  if (!codigo) { out.textContent = "Primeiro abra ou crie uma casa (passo 1)."; return; }
  try {
    const r = await api("/api/conferir", { method: "POST",
      body: JSON.stringify({ kwh_cobrado: Number($("cobrado").value), dias: Number($("dias").value) }) });
    const pct = (r.divergencia * 100).toFixed(0);
    out.replaceChildren();
    const p = document.createElement("p");
    p.innerHTML = r.resultado === "ok"
      ? `<span class="ok">A conta parece correta.</span> Esperado: ${r.kwh_esperado} kWh (diferença de ${pct}%).`
      : `<span class="bad">A conta está ${r.divergencia > 0 ? "acima" : "abaixo"} do que o seu medidor indica (${pct}%).</span>
         Esperado: ${r.kwh_esperado} kWh (${brl(r.valor_esperado_reais)}); cobrado: ${brl(r.valor_cobrado_estimado_reais)}.
         Pode ter sido cobrança por média ou erro de leitura. Use o texto abaixo no canal oficial da distribuidora.`;
    out.append(p);
    if (r.contestacao) {
      const t = document.createElement("textarea");
      t.rows = 12; t.readOnly = true; t.value = r.contestacao; t.setAttribute("aria-label", "Texto da contestação");
      const b = document.createElement("button");
      b.type = "button"; b.className = "sec"; b.textContent = "Copiar texto";
      b.onclick = () => navigator.clipboard.writeText(t.value).then(() => { b.textContent = "Copiado"; });
      out.append(t, b);
    }
  } catch (e) { out.textContent = "Não foi possível conferir: " + e.message; }
};

$("painel").onclick = async () => {
  const out = $("saida-painel");
  try {
    const r = await fetch("/api/admin/painel", { headers: { Authorization: "Bearer " + $("token").value } });
    if (!r.ok) throw new Error(r.status === 401 ? "token inválido" : "erro " + r.status);
    const d = await r.json();
    out.textContent = `Casas: ${d.casas} · leituras: ${d.leituras} · conferências: ${d.conferencias} · ` +
      `divergentes: ${d.conferencias_divergentes} (em ${d.casas_com_divergencia} casas)`;
  } catch (e) { out.textContent = "Não foi possível abrir o painel: " + e.message; }
};

async function carregarPiloto() {
  try {
    const r = await fetch("/api/dados/painel");
    if (!r.ok) throw new Error("sem dados");
    const p = await r.json();
    const ref = p.referencia_tarifaria;
    const num = (v, casas = 1) => v == null ? "—" : v.toLocaleString("pt-BR", { maximumFractionDigits: casas });
    $("piloto-ref").textContent =
      `Dados até ${new Date(p.data_referencia + "T12:00").toLocaleDateString("pt-BR")}. ` +
      `Tarifa residencial ${ref.distribuidora}: R$ ${num(ref.tarifa_residencial_rs_kwh, 3)}/kWh sem impostos; ` +
      `bandeira ${ref.bandeira}` + (p.demonstracao ? ". Leituras e contas de demonstração." : ".");
    $("piloto").replaceChildren(...p.bairros.map((b) => {
      const tr = document.createElement("tr");
      const valores = [b.bairro, b.familias, num(b.consumo_medio_diario_kwh),
        b.conta_media_estimada_30d_rs == null ? "—" : brl(b.conta_media_estimada_30d_rs),
        b.familias_consumo_acima_do_habitual, b.contas_conferidas, b.contas_divergentes, brl(b.valor_a_contestar_rs)];
      valores.forEach((v, i) => {
        const td = document.createElement(i === 0 ? "th" : "td");
        if (i === 0) td.scope = "row";
        td.textContent = v; tr.append(td);
      });
      return tr;
    }));
    $("piloto-fonte").textContent = `Fonte: ${p.tabela_origem} (${p.fontes.join(", ")}). Só totais por bairro, sem dado pessoal.`;
  } catch (_) {
    $("piloto-ref").textContent = "Painel do piloto ainda não publicado.";
  }
}

carregarPiloto();
const hoje = new Date(); hoje.setMinutes(hoje.getMinutes() - hoje.getTimezoneOffset()); // data local, nao UTC
$("data").value = hoje.toISOString().slice(0, 10);
fetch("/api/config").then((r) => r.json()).then((c) => { $("versao").textContent = String(c.version).slice(0, 7); }).catch(() => {});
if (location.hash === "#demonstracao") lembrar("demonstracao"); // link direto para a casa de demonstracao
else try { const salvo = localStorage.getItem(CHAVE); if (salvo) lembrar(salvo); } catch (_) { /* sem armazenamento */ }
