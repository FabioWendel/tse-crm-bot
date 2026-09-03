from __future__ import annotations

import csv
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


TOTAL_OPERADORES = 4
PENDENTES_API_URL = "https://juniorveloso.com.br/cadastrante/api/validar-local"
PENDENTES_POR_PAGINA = 100
MAX_PAGINAS_API = 1000

ABAS_CRM_API_CONFIRMADAS = {
    "Pendentes": "pendentes",
}
ABAS_CRM_API_AGUARDANDO_CONFIRMACAO = (
    "Já validados",
    "Fora da cidade",
    "Dados incompletos",
    "Não encontrados",
    "Revisar",
)

ULTIMO_TOTAL_PENDENTES_API = 0
ULTIMOS_ITENS_INVALIDOS: list[dict[str, str]] = []
ULTIMOS_DUPLICADOS_API = 0

VALORES_SEM_DADO = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "null",
    "undefined",
    "sem registro",
    "sem valor",
    "sem dados",
    "não informado",
    "nao informado",
    "não consta",
    "nao consta",
    "sem informação",
    "sem informacao",
}

TERMOS_ERRO_TECNICO = (
    "ERRO NAO IDENTIFICADO",
    "ERRO NÃO IDENTIFICADO",
    "CAPTCHA",
    "TIMEOUT",
    "TEMPO ESGOTADO",
    "TELA TRAVADA",
    "NAO VEIO RESPOSTA",
    "NÃO VEIO RESPOSTA",
    "NAO CONSEGUI",
    "NÃO CONSEGUI",
    "INDISPONIVEL",
    "INDISPONÍVEL",
    "FALHA TECNICA",
    "FALHA TÉCNICA",
)

PADRAO_CPF_BOT_ERROR = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"\s*\|\s*CPF\s+(\d{11})\s*\|\s*tentativa\s+\d+\s*$",
    re.IGNORECASE,
)

PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent

PASTA_OPERADORES = RAIZ / "multi-instancia" / "dados"
PASTA_DADOS = PASTA_ATUAL / "dados"

AUDITORIA_CSV = PASTA_DADOS / "auditoria_varredura.csv"
FILA_CSV = PASTA_DADOS / "fila_varredura.csv"

os.environ["TSE_NAVEGADOR"] = "brave"

sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


# ------------------------------------------------------------
# Perfil exclusivo desta versão.
# Não conflita com operador 0..3.
# ------------------------------------------------------------

bot.BASE_DIR = PASTA_DADOS
bot.PROFILE_DIR = PASTA_DADOS / "perfil-crm"
bot.TSE_PROFILE_DIR = PASTA_DADOS / "perfil-tse-brave"
bot.LOG_FILE = PASTA_DADOS / "consultas.csv"
bot.ERROR_LOG = PASTA_DADOS / "bot_error.log"
bot.DETAIL_LOG = PASTA_DADOS / "execucao_detalhada.txt"

bot.TSE_NAVEGADOR = "brave"
bot.TSE_REMOTE_DEBUGGING_PORT = 9360


def normalizar(valor: object) -> str:
    return re.sub(r"\s+", " ", str(valor or "").strip().lower())


def campo_valido(valor: object) -> bool:
    return normalizar(valor) not in VALORES_SEM_DADO


def cpf_valido(valor: object) -> bool:
    return len(bot.only_digits(str(valor or ""))) == 11


def nascimento_valido(valor: object) -> bool:
    return bool(normalizar_nascimento(valor))


def normalizar_nascimento(valor: object) -> str:
    texto = str(valor or "").strip()
    if normalizar(texto) in VALORES_SEM_DADO:
        return ""

    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def dados_minimos_validos(pessoa: bot.Pessoa) -> tuple[bool, str]:
    problemas: list[str] = []

    if not campo_valido(pessoa.nome):
        problemas.append("nome")

    if not cpf_valido(pessoa.cpf):
        problemas.append("cpf")

    if not campo_valido(pessoa.mae):
        problemas.append("nome da mãe")

    if not nascimento_valido(pessoa.nascimento):
        problemas.append("nascimento")

    if problemas:
        return False, ", ".join(problemas)

    return True, ""


# ------------------------------------------------------------
# HISTÓRICO DOS 4 OPERADORES
# ------------------------------------------------------------


def registro_tecnico(linha: dict[str, str]) -> bool:
    texto = " | ".join(
        str(linha.get(campo) or "")
        for campo in ("status", "comunicado", "resultado")
    ).upper()

    return any(termo in texto for termo in TERMOS_ERRO_TECNICO)


def ler_historico() -> tuple[set[str], set[str], set[str], Counter]:
    """
    concluídos:
        consulta teve resposta conclusiva.
        Inclui TSE encontrado e "Não achei" verdadeiro.

    tecnicos:
        consulta não chegou a concluir por CAPTCHA, timeout etc.,
        registrado no CSV ou no bot_error.log.

    Qualquer conclusão registrada no CSV vence erros anteriores e também
    evita que um erro posterior de inativação rebaixe o CPF para falha.
    """

    concluidos: set[str] = set()
    tecnicos: set[str] = set()

    contadores: Counter = Counter()

    for operador in range(TOTAL_OPERADORES):
        caminho = (
            PASTA_OPERADORES
            / f"operador-{operador}"
            / "consultas.csv"
        )

        print(f"Lendo operador {operador}: {caminho}")

        if not caminho.exists():
            print("  AVISO: consultas.csv não encontrado.")
            contadores["csv_ausente"] += 1
            continue

        with caminho.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as arquivo:

            leitor = csv.DictReader(arquivo)

            for linha in leitor:
                cpf = bot.only_digits(
                    str(linha.get("cpf") or "")
                )

                if len(cpf) != 11:
                    contadores["linha_invalida"] += 1
                    continue

                contadores["linhas"] += 1

                if registro_tecnico(linha):
                    tecnicos.add(cpf)
                else:
                    concluidos.add(cpf)

    realizados_csv = concluidos | tecnicos
    contadores["cpfs_csv"] = len(realizados_csv)
    erros_log: set[str] = set()

    for operador in range(TOTAL_OPERADORES):
        caminho = (
            PASTA_OPERADORES
            / f"operador-{operador}"
            / "bot_error.log"
        )

        print(f"Lendo erros do operador {operador}: {caminho}")

        if not caminho.exists():
            print("  AVISO: bot_error.log não encontrado.")
            contadores["log_ausente"] += 1
            continue

        with caminho.open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as arquivo:
            for linha in arquivo:
                correspondencia = PADRAO_CPF_BOT_ERROR.match(linha.strip())
                if not correspondencia:
                    continue
                erros_log.add(correspondencia.group(1))
                contadores["linhas_erro_log"] += 1

    contadores["cpfs_erro_log"] = len(erros_log)
    erros_sem_conclusao = erros_log - concluidos
    contadores["cpfs_erro_log_sem_conclusao"] = len(erros_sem_conclusao)
    contadores["cpfs_adicionados_pelo_log"] = len(erros_sem_conclusao - tecnicos)
    tecnicos.update(erros_sem_conclusao)

    # Uma conclusão no CSV é definitiva. Isso também protege o caso em que
    # o local foi salvo e somente a inativação posterior gerou bot_error.log.
    tecnicos -= concluidos

    return concluidos, tecnicos, realizados_csv, contadores


# ------------------------------------------------------------
# FOTOGRAFIA DE PENDENTES PELA API AUTENTICADA
# ------------------------------------------------------------


def inventariar_pendentes_api(page: Page) -> list[bot.Pessoa]:
    global ULTIMO_TOTAL_PENDENTES_API, ULTIMOS_ITENS_INVALIDOS, ULTIMOS_DUPLICADOS_API

    ULTIMO_TOTAL_PENDENTES_API = 0
    ULTIMOS_ITENS_INVALIDOS = []
    ULTIMOS_DUPLICADOS_API = 0

    por_id: dict[int, bot.Pessoa] = {}
    cpfs_vistos: set[str] = set()
    ids_pagina_anterior: tuple[str, ...] | None = None

    for numero_pagina in range(1, MAX_PAGINAS_API + 1):
        resposta = None
        try:
            resposta = page.request.get(
                PENDENTES_API_URL,
                params={
                    "aba": ABAS_CRM_API_CONFIRMADAS["Pendentes"],
                    "q": "",
                    "page": numero_pagina,
                    "per_page": PENDENTES_POR_PAGINA,
                    "sort": "nome",
                    "dir": "asc",
                },
                headers={"Accept": "application/json"},
                timeout=120000,
            )
            if not resposta.ok:
                raise RuntimeError(
                    f"API de Pendentes respondeu HTTP {resposta.status} na página {numero_pagina}."
                )

            payload = resposta.json()
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
                items = payload["items"]
            else:
                raise RuntimeError(
                    f"API de Pendentes devolveu JSON inesperado na página {numero_pagina}."
                )

            ULTIMO_TOTAL_PENDENTES_API += len(items)
            ids_pagina = tuple(
                str(item.get("id") if isinstance(item, dict) else "<item-inválido>")
                for item in items
            )
            if items and ids_pagina == ids_pagina_anterior:
                raise RuntimeError(
                    f"API de Pendentes repetiu exatamente os IDs da página {numero_pagina - 1}; "
                    "a fotografia foi abortada para não gerar uma fila incompleta."
                )
            ids_pagina_anterior = ids_pagina

            antes = len(por_id)
            for indice, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    ULTIMOS_ITENS_INVALIDOS.append({
                        "id": "",
                        "cpf": "",
                        "nome": "",
                        "observacao": f"Página {numero_pagina}, item {indice}: não é um objeto JSON.",
                    })
                    continue

                try:
                    crm_id = int(item.get("id"))
                except (TypeError, ValueError):
                    crm_id = 0
                cpf = bot.only_digits(str(item.get("cpf") or ""))

                problemas: list[str] = []
                if crm_id <= 0:
                    problemas.append("ID ausente ou inválido")
                if len(cpf) != 11:
                    problemas.append("CPF ausente ou inválido")
                if problemas:
                    ULTIMOS_ITENS_INVALIDOS.append({
                        "id": str(item.get("id") or ""),
                        "cpf": cpf,
                        "nome": str(item.get("nome") or "").strip(),
                        "observacao": f"Página {numero_pagina}, item {indice}: {', '.join(problemas)}.",
                    })
                    continue

                if crm_id in por_id or cpf in cpfs_vistos:
                    ULTIMOS_DUPLICADOS_API += 1
                    continue

                pessoa = bot.Pessoa(
                    row_index=-1,
                    nome=str(item.get("nome") or "").strip(),
                    cpf=cpf,
                    mae=str(item.get("nome_da_mae") or "").strip(),
                    nascimento=normalizar_nascimento(item.get("nascimento")),
                    crm_id=crm_id,
                )
                por_id[crm_id] = pessoa
                cpfs_vistos.add(cpf)

            novos = len(por_id) - antes
            print(f"API Pendentes: página {numero_pagina}, +{novos}, total {len(por_id)}")

            if len(items) < PENDENTES_POR_PAGINA:
                return list(por_id.values())
            if numero_pagina == MAX_PAGINAS_API:
                raise RuntimeError(
                    f"API de Pendentes atingiu o limite de segurança de {MAX_PAGINAS_API} páginas."
                )
        finally:
            if resposta is not None:
                resposta.dispose()

    return list(por_id.values())


# ------------------------------------------------------------
# AUDITORIA
# ------------------------------------------------------------


def salvar_csv(
    caminho: Path,
    registros: list[dict[str, str]],
    campos: list[str],
) -> None:

    caminho.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with caminho.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as arquivo:

        writer = csv.DictWriter(
            arquivo,
            fieldnames=campos,
        )

        writer.writeheader()
        writer.writerows(registros)


# ------------------------------------------------------------
# MONTAGEM DA FILA
# ------------------------------------------------------------

def montar_fila(
    base_total: list[bot.Pessoa],
    pendentes: list[bot.Pessoa],
    concluidos: set[str],
    tecnicos: set[str],
    base_total_disponivel: bool = True,
) -> tuple[
    list[bot.Pessoa],
    list[dict[str, str]],
    Counter,
]:

    fila: list[bot.Pessoa] = []
    auditoria: list[dict[str, str]] = []
    contagem: Counter = Counter()

    pendentes_por_cpf = {
        pessoa.cpf: pessoa
        for pessoa in pendentes
        if cpf_valido(pessoa.cpf)
    }
    base_por_cpf = (
        {
            pessoa.cpf: pessoa
            for pessoa in base_total
            if cpf_valido(pessoa.cpf)
        }
        if base_total_disponivel
        else dict(pendentes_por_cpf)
    )

    for item_invalido in ULTIMOS_ITENS_INVALIDOS:
        cpf = item_invalido["cpf"]
        auditoria.append({
            "id": item_invalido["id"],
            "cpf": cpf,
            "nome": item_invalido["nome"],
            "aba_atual": "Pendentes",
            "historico_4_operadores": classificar_historico(
                cpf,
                concluidos,
                tecnicos,
            ),
            "dados_validos": "NÃO",
            "acao": "ITEM_API_INVALIDO",
            "observacao": item_invalido["observacao"],
        })

    for cpf, referencia_base in base_por_cpf.items():
        historico = classificar_historico(cpf, concluidos, tecnicos)
        pessoa = pendentes_por_cpf.get(cpf)

        if pessoa is None:
            auditoria.append({
                "id": str(referencia_base.crm_id or ""),
                "cpf": cpf,
                "nome": referencia_base.nome,
                "aba_atual": "FORA_DE_PENDENTES",
                "historico_4_operadores": historico,
                "dados_validos": "NÃO APLICÁVEL",
                "acao": "ENCERRADO_ESTADO_ATUAL",
                "observacao": (
                    "CPF não está na fotografia atual de Pendentes; "
                    "o estado atual do CRM prevalece sobre o CSV."
                ),
            })
            contagem["fora_de_pendentes"] += 1
            continue

        contagem[f"pendente_{historico}"] += 1
        valido, problema = dados_minimos_validos(pessoa)

        if not valido:
            contagem["dados_insuficientes"] += 1
            auditoria.append({
                "id": str(pessoa.crm_id or ""),
                "cpf": cpf,
                "nome": pessoa.nome,
                "aba_atual": "Pendentes",
                "historico_4_operadores": historico,
                "dados_validos": "NÃO",
                "acao": "DADOS_INSUFICIENTES",
                "observacao": f"Campos ausentes ou inválidos: {problema}.",
            })
            continue

        fila.append(pessoa)
        contagem["fila"] += 1

        auditoria.append({
            "id": str(pessoa.crm_id or ""),
            "cpf": cpf,
            "nome": pessoa.nome,
            "aba_atual": "Pendentes",
            "historico_4_operadores": historico,
            "dados_validos": "SIM",
            "acao": "PROCESSAR",
            "observacao": "",
        })

    if base_total_disponivel:
        for cpf, pessoa in pendentes_por_cpf.items():
            if cpf in base_por_cpf:
                continue
            auditoria.append({
                "id": str(pessoa.crm_id or ""),
                "cpf": cpf,
                "nome": pessoa.nome,
                "aba_atual": "Pendentes",
                "historico_4_operadores": classificar_historico(
                    cpf,
                    concluidos,
                    tecnicos,
                ),
                "dados_validos": "NÃO APLICÁVEL",
                "acao": "FORA_DA_BASE_TOTAL",
                "observacao": (
                    "CPF apareceu em Pendentes, mas não existe na fotografia "
                    "da base total; não foi incluído na fila."
                ),
            })
            contagem["pendente_fora_base"] += 1

    return fila, auditoria, contagem


def classificar_historico(
    cpf: str,
    concluidos: set[str],
    tecnicos: set[str],
) -> str:
    if cpf in concluidos:
        return "CONCLUIDO"
    if cpf in tecnicos:
        return "FALHA_TECNICA"
    return "NAO_PASSOU_NOS_4_OPERADORES"


def origem_historico(historico: str) -> str:
    return {
        "FALHA_TECNICA": "falha_tecnica",
        "CONCLUIDO": "concluido_ainda_pendente",
        "NAO_PASSOU_NOS_4_OPERADORES": "nao_passou_nos_4_operadores",
    }[historico]

# ------------------------------------------------------------
# EXECUÇÃO
# ------------------------------------------------------------


def perguntar_execucao() -> tuple[int, int]:
    pausar = bot.ler_inteiro(
        "Fazer pausa a cada quantas pessoas? "
        "[Enter = 10] ",
        minimo=1,
        maximo=100000,
        padrao=10,
    )

    segundos = bot.ler_inteiro(
        "Quantos segundos deve durar cada pausa? "
        "[Enter = 10] ",
        minimo=1,
        maximo=3600,
        padrao=10,
    )

    return pausar, segundos


def executar() -> int:
    PASTA_DADOS.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("=" * 72)
    print(" CRM x TSE - VARREDURA FINAL")
    print("=" * 72)

    concluidos, tecnicos, realizados_csv, hist = ler_historico()

    print()
    print(
        f"Linhas lidas nos CSVs:         "
        f"{hist['linhas']}"
    )
    print(
        f"CPFs concluídos:               "
        f"{len(concluidos)}"
    )
    print(
        f"CPFs com falha técnica:        "
        f"{len(tecnicos)}"
    )
    print(
        f"CPFs encontrados nos logs:     "
        f"{hist['cpfs_erro_log']}"
    )
    print(
        f"Falhas acrescentadas pelos logs: "
        f"{hist['cpfs_adicionados_pelo_log']}"
    )

    try:
        with sync_playwright() as playwright:

            context = (
                playwright.chromium
                .launch_persistent_context(
                    str(bot.PROFILE_DIR),
                    executable_path=(
                        bot.find_chrome_executable()
                    ),
                    headless=bot.HEADLESS,
                    slow_mo=bot.SLOW_MO_MS,
                    viewport={
                        "width": 1366,
                        "height": 768,
                    },
                    args=["--start-maximized"],
                )
            )

            crm = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            if not bot.ir_para(
                crm,
                bot.CRM_URL,
            ):
                raise RuntimeError(
                    "Não consegui abrir o CRM."
                )

            bot.ensure_crm_ready(crm)

            print()
            print(
                "Fotografando a base total pela API autenticada do CRM..."
            )
            base_total = bot.inventariar_eleitores_api(crm)
            base_total_disponivel = bool(base_total)
            if not base_total_disponivel:
                print(
                    "AVISO: a API /cadastrante/api/eleitores permaneceu "
                    "indisponível após as tentativas. A varredura continuará "
                    "usando a fotografia de Pendentes."
                )

            print()
            print(
                "Fotografando todos os Pendentes "
                "pela API autenticada do CRM..."
            )

            pendentes = inventariar_pendentes_api(crm)

            print()
            print(
                f"Pendentes retornados pela API: "
                f"{ULTIMO_TOTAL_PENDENTES_API}"
            )

            fila, auditoria, contagem = (
                montar_fila(
                    base_total,
                    pendentes,
                    concluidos,
                    tecnicos,
                    base_total_disponivel=base_total_disponivel,
                )
            )

            salvar_csv(
                AUDITORIA_CSV,
                auditoria,
                [
                    "id",
                    "cpf",
                    "nome",
                    "aba_atual",
                    "historico_4_operadores",
                    "dados_validos",
                    "acao",
                    "observacao",
                ],
            )

            salvar_csv(
                FILA_CSV,
                [
                    {
                        "id": str(p.crm_id or ""),
                        "cpf": p.cpf,
                        "nome": p.nome,
                        "mae": p.mae,
                        "nascimento": p.nascimento,
                        "origem": origem_historico(
                            classificar_historico(
                                p.cpf,
                                concluidos,
                                tecnicos,
                            )
                        ),
                    }
                    for p in fila
                ],
                [
                    "id",
                    "cpf",
                    "nome",
                    "mae",
                    "nascimento",
                    "origem",
                ],
            )

            print()
            print("=" * 72)
            print(" RESULTADO DA VARREDURA")
            print("=" * 72)

            if base_total_disponivel:
                cpfs_base = {
                    pessoa.cpf
                    for pessoa in base_total
                    if cpf_valido(pessoa.cpf)
                }
                print(
                    f"Total da base:                       "
                    f"{len(base_total)}"
                )
            else:
                cpfs_base = set()
                print(
                    "Total da base:                       "
                    "INDISPONIVEL (API /eleitores HTTP 500)"
                )

            print(
                f"CPFs realizados nos 4 operadores:   "
                f"{len(realizados_csv)}"
            )

            print(
                f"CPFs únicos nos bot_error.log:      "
                f"{hist['cpfs_erro_log']}"
            )

            if base_total_disponivel:
                print(
                    f"Residual inicial pelo histórico:    "
                    f"{len(cpfs_base - realizados_csv)}"
                )
            else:
                print(
                    "Residual inicial pelo histórico:    "
                    "NAO VERIFICADO"
                )

            print(
                f"Pendentes atuais:                    "
                f"{ULTIMO_TOTAL_PENDENTES_API}"
            )

            print(
                f"Itens inválidos ignorados:           "
                f"{len(ULTIMOS_ITENS_INVALIDOS)}"
            )

            print("-" * 72)

            print(
                f"Pendentes que falharam tecnicamente: "
                f"{contagem['pendente_FALHA_TECNICA']}"
            )

            print(
                f"Concluídos mas ainda Pendentes:      "
                f"{contagem['pendente_CONCLUIDO']}"
            )

            print(
                f"Não passaram nos 4 operadores:       "
                f"{contagem['pendente_NAO_PASSOU_NOS_4_OPERADORES']}"
            )

            print(
                f"Dados insuficientes:                 "
                f"{contagem['dados_insuficientes']}"
            )

            if base_total_disponivel:
                print(
                    f"Pendentes fora da base total:        "
                    f"{contagem['pendente_fora_base']}"
                )
            else:
                print(
                    "Pendentes fora da base total:        "
                    "NAO VERIFICADO"
                )

            print()
            print(
                f"FILA REAL:                           "
                f"{len(fila)}"
            )

            print()
            print(
                "Demais abas não consultadas: os valores exatos do parâmetro "
                "'aba' ainda precisam ser confirmados para "
                + ", ".join(ABAS_CRM_API_AGUARDANDO_CONFIRMACAO)
                + "."
            )

            print()
            print(
                f"Auditoria salva em:\n"
                f"{AUDITORIA_CSV}"
            )

            print()
            print(
                f"Fila salva em:\n"
                f"{FILA_CSV}"
            )

            print("=" * 72)

            if not fila:
                print(
                    "\nNenhuma pessoa precisa "
                    "ser consultada."
                )
                context.close()
                return 0

            resposta = input(
                "\nDigite S para começar... "
                "Qualquer outra tecla "
                "encerra apenas com a auditoria: "
            ).strip().upper()

            if resposta != "S":
                print(
                    "\nVarredura encerrada sem "
                    "consultar o TSE."
                )
                context.close()
                return 0

            limite = bot.ler_inteiro(
                "Quantos CPFs processar agora? "
                "[Enter/0 = todos] ",
                minimo=0,
                maximo=100000,
                padrao=0,
            )

            if limite > 0:
                fila = fila[:limite]

            pausar, segundos = perguntar_execucao()

            bot.TSE_PAUSAR_A_CADA_PESSOAS = (
                pausar
            )

            bot.TSE_DURACAO_PAUSA_MS = (
                segundos * 1000
            )

            print()
            print(
                "Iniciando processamento."
            )

            print(
                "Antes de cada consulta, "
                "o motor normal confirma "
                "novamente o CPF em Pendentes."
            )

            falhas = bot.rodar_fila(
                playwright,
                context,
                crm,
                fila,
                "varredura ",
            )

            if (
                falhas
                and not bot._PERFIL_TSE_BLOQUEADO
            ):
                print()
                print(
                    f"{len(falhas)} pessoa(s) "
                    "falharam."
                )

                print(
                    "Executando um repasse final."
                )

                falhas = bot.rodar_fila(
                    playwright,
                    context,
                    crm,
                    falhas,
                    "repasse-varredura ",
                )

            print()
            print("=" * 72)
            print(" VARREDURA FINALIZADA")
            print("=" * 72)

            print(
                f"Fila executada: "
                f"{len(fila)}"
            )

            print(
                f"Sem sucesso após repasse: "
                f"{len(falhas)}"
            )

            if falhas:
                print()
                print(
                    "Ainda pendentes:"
                )

                for pessoa in falhas:
                    print(
                        f"  CPF {pessoa.cpf} "
                        f"- {pessoa.nome}"
                    )

            context.close()

            return 0

    except KeyboardInterrupt:
        print(
            "\nInterrompido por você. "
            "Os arquivos existentes foram preservados."
        )

        return 130

    except Exception:
        PASTA_DADOS.mkdir(
            parents=True,
            exist_ok=True,
        )

        with bot.ERROR_LOG.open(
            "a",
            encoding="utf-8",
        ) as arquivo:

            arquivo.write(
                f"\n{'=' * 70}\n"
                f"{datetime.now().isoformat(timespec='seconds')}"
                " | erro geral varredura\n"
            )

            arquivo.write(
                traceback.format_exc()
            )

        print()
        print(
            "ERRO NA VARREDURA."
        )

        print(
            "\n".join(
                traceback.format_exc()
                .splitlines()[-10:]
            )
        )

        print(
            f"\nLog completo: "
            f"{bot.ERROR_LOG}"
        )

        return 1

    finally:
        bot.encerrar_sessao_tse()


if __name__ == "__main__":
    raise SystemExit(executar())
