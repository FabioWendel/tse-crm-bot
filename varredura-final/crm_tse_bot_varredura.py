from __future__ import annotations

import csv
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright


TOTAL_OPERADORES = 4

ABA_PENDENTES = "Pendentes"

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
    texto = normalizar(valor)

    if texto in VALORES_SEM_DADO:
        return False

    formatos = (
        r"\d{2}/\d{2}/\d{4}",
        r"\d{2}-\d{2}-\d{4}",
        r"\d{4}-\d{2}-\d{2}",
    )

    return any(re.fullmatch(formato, texto) for formato in formatos)


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


def ler_historico() -> tuple[set[str], set[str], Counter]:
    """
    concluídos:
        consulta teve resposta conclusiva.
        Inclui TSE encontrado e "Não achei" verdadeiro.

    tecnicos:
        consulta não chegou a concluir por CAPTCHA, timeout etc.

    Se um CPF falhou primeiro mas depois teve resposta conclusiva,
    a conclusão vence.
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

    # Uma conclusão posterior é definitiva.
    tecnicos -= concluidos

    return concluidos, tecnicos, contadores


# ------------------------------------------------------------
# NAVEGAÇÃO DAS ABAS
# ------------------------------------------------------------


def clicar_aba(page: Page, nome: str) -> bool:
    candidatos = (
        page.get_by_role(
            "button",
            name=re.compile(re.escape(nome), re.I),
        ).first,
        page.get_by_role(
            "tab",
            name=re.compile(re.escape(nome), re.I),
        ).first,
        page.get_by_text(
            re.compile(
                rf"^\s*{re.escape(nome)}(?:\s+\d+)?\s*$",
                re.I,
            )
        ).first,
    )

    for candidato in candidatos:
        try:
            if not candidato.is_visible(timeout=1500):
                continue

            candidato.click(timeout=8000)
            page.wait_for_timeout(1200)

            return True

        except (TimeoutError, Error):
            continue

    return False


def primeira_linha(page: Page) -> str:
    try:
        return bot.clean(
            page.locator("table tbody tr")
            .first
            .inner_text(timeout=4000)
        )
    except (TimeoutError, Error):
        return ""


def voltar_primeira_pagina(page: Page) -> None:
    candidatos = (
        page.get_by_role(
            "button",
            name=re.compile(r"primeir", re.I),
        ).first,
        page.get_by_role(
            "link",
            name=re.compile(r"primeir", re.I),
        ).first,
        page.locator("button, a").filter(
            has_text=re.compile(r"^\s*(«|<<)\s*$")
        ).first,
    )

    for candidato in candidatos:
        try:
            if (
                candidato.is_visible(timeout=700)
                and candidato.is_enabled(timeout=700)
            ):
                candidato.click(timeout=5000)
                page.wait_for_timeout(1000)
                return

        except (TimeoutError, Error):
            continue


def proxima_pagina(page: Page) -> bool:
    anterior = primeira_linha(page)

    candidatos = (
        page.get_by_role(
            "button",
            name=re.compile(
                r"^(pr[óo]xim[ao]|next|seguinte)",
                re.I,
            ),
        ).first,
        page.get_by_role(
            "link",
            name=re.compile(
                r"^(pr[óo]xim[ao]|next|seguinte)",
                re.I,
            ),
        ).first,
        page.locator(
            "button[aria-label*='rox' i], "
            "a[aria-label*='rox' i]"
        ).first,
        page.locator("button, a").filter(
            has_text=re.compile(r"^\s*(›|»|>)\s*$")
        ).first,
    )

    for candidato in candidatos:
        try:
            if not candidato.is_visible(timeout=700):
                continue

            if not candidato.is_enabled(timeout=700):
                continue

            candidato.click(timeout=6000)
            page.wait_for_timeout(1200)

            atual = primeira_linha(page)

            return atual != anterior

        except (TimeoutError, Error):
            continue

    return False


def extrair_cpf_da_linha(texto: str) -> str:
    candidatos = re.findall(
        r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"
        r"|\b\d{11}\b",
        texto,
    )

    for candidato in candidatos:
        cpf = bot.only_digits(candidato)

        if len(cpf) == 11:
            return cpf

    return ""


def inventariar_aba(page: Page, aba: str) -> set[str]:
    print()
    print(f">>> Varrendo aba: {aba}")

    if not clicar_aba(page, aba):
        raise RuntimeError(
            f"Não consegui abrir a aba {aba!r}."
        )

    voltar_primeira_pagina(page)
    bot.maximizar_por_pagina(page)

    cpfs: set[str] = set()

    for pagina in range(1, 1001):
        rows = page.locator("table tbody tr")
        quantidade = rows.count()

        antes = len(cpfs)

        for indice in range(quantidade):
            row = rows.nth(indice)

            try:
                texto = row.inner_text(timeout=5000)
            except (TimeoutError, Error):
                continue

            cpf = extrair_cpf_da_linha(texto)

            if cpf:
                cpfs.add(cpf)

        novos = len(cpfs) - antes

        print(
            f"  página {pagina}: "
            f"+{novos} CPF(s), total {len(cpfs)}"
        )

        if not proxima_pagina(page):
            break

    return cpfs

# ------------------------------------------------------------
# VALIDAÇÃO AO VIVO DOS PENDENTES
# ------------------------------------------------------------


def carregar_pendente_atual(
    page: Page,
    referencia: bot.Pessoa,
) -> tuple[bot.Pessoa | None, str]:

    """
    Usa o próprio mecanismo do bot para buscar pelo CPF.

    Isso é importante porque outra máquina pode alterar o CRM
    entre a fotografia inicial e o momento de processamento.
    """

    try:
        pessoa = bot.abrir_pessoa_por_cpf(
            page,
            referencia,
        )
    except Exception as exc:
        return None, f"erro ao reler CPF: {exc}"

    if pessoa is None:
        return None, "não está mais em Pendentes"

    valido, motivo = dados_minimos_validos(pessoa)

    if not valido:
        return None, f"dados insuficientes: {motivo}"

    return pessoa, ""


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
    page: Page,
    base_api: list[bot.Pessoa],
    cpfs_pendentes: set[str],
    concluidos: set[str],
    tecnicos: set[str],
) -> tuple[
    list[bot.Pessoa],
    list[dict[str, str]],
    Counter,
]:

    fila: list[bot.Pessoa] = []
    auditoria: list[dict[str, str]] = []
    contagem: Counter = Counter()

    # Mapa rápido da fotografia inicial da API.
    base_por_cpf = {
        pessoa.cpf: pessoa
        for pessoa in base_api
        if pessoa.cpf
    }

    print()
    print(
        f"CPFs presentes em Pendentes agora: "
        f"{len(cpfs_pendentes)}"
    )

    for indice, cpf in enumerate(
        sorted(cpfs_pendentes),
        start=1,
    ):
        referencia = base_por_cpf.get(cpf)

        if cpf in concluidos:
            historico = "CONCLUIDO"
        elif cpf in tecnicos:
            historico = "FALHA_TECNICA"
        else:
            historico = "NOVO"

        if referencia is None:
            contagem["sem_api"] += 1

            auditoria.append({
                "cpf": cpf,
                "historico": historico,
                "aba_atual": "Pendentes",
                "acao": "SEM_REGISTRO_API",
                "observacao": (
                    "CPF apareceu em Pendentes, mas não estava "
                    "na fotografia inicial da API."
                ),
            })

            continue

        print(
            f"\nValidando [{indice}/{len(cpfs_pendentes)}] "
            f"CPF {cpf}"
        )

        pessoa, problema = carregar_pendente_atual(
            page,
            referencia,
        )

        if pessoa is None:
            if problema == "não está mais em Pendentes":
                contagem["movido_durante_varredura"] += 1

                auditoria.append({
                    "cpf": cpf,
                    "historico": historico,
                    "aba_atual": "Pendentes",
                    "acao": "MOVIDO_DURANTE_VARREDURA",
                    "observacao": (
                        "Outra máquina ou processo retirou "
                        "o CPF de Pendentes durante a auditoria."
                    ),
                })

            else:
                contagem["dados_insuficientes"] += 1

                auditoria.append({
                    "cpf": cpf,
                    "historico": historico,
                    "aba_atual": "Pendentes",
                    "acao": "DADOS_INSUFICIENTES",
                    "observacao": problema,
                })

            continue

        fila.append(pessoa)

        if historico == "FALHA_TECNICA":
            contagem["fila_FALHA_TECNICA"] += 1

        elif historico == "CONCLUIDO":
            # O histórico dizia concluído, mas o CRM atual ainda
            # mantém a pessoa em Pendentes. O estado atual vence.
            contagem["fila_CONCLUIDO_PENDENTE"] += 1

        else:
            contagem["fila_NOVO"] += 1

        contagem["fila"] += 1

        auditoria.append({
            "cpf": cpf,
            "historico": historico,
            "aba_atual": "Pendentes",
            "acao": "PROCESSAR",
            "observacao": "",
        })

    return fila, auditoria, contagem

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

    concluidos, tecnicos, hist = ler_historico()

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
                "Obtendo fotografia completa "
                "da API do CRM..."
            )

            base_api = (
                bot.inventariar_eleitores_api(crm)
            )

            if not base_api:
                raise RuntimeError(
                    "A API não devolveu a base completa."
                )

            print(
                f"Total atual da API: "
                f"{len(base_api)}"
            )

            print()
            print(
                "Fotografando somente a aba Pendentes..."
            )

            cpfs_pendentes = inventariar_aba(
                crm,
                ABA_PENDENTES,
            )

            print()
            print(
                f"Pendentes encontrados: "
                f"{len(cpfs_pendentes)}"
            )

            fila, auditoria, contagem = (
                montar_fila(
                    crm,
                    base_api,
                    cpfs_pendentes,
                    concluidos,
                    tecnicos,
                )
            )

            salvar_csv(
                AUDITORIA_CSV,
                auditoria,
                [
                    "cpf",
                    "historico",
                    "aba_atual",
                    "acao",
                    "observacao",
                ],
            )

            salvar_csv(
                FILA_CSV,
                [
                    {
                        "cpf": p.cpf,
                        "nome": p.nome,
                        "mae": p.mae,
                        "nascimento": p.nascimento,
                        "origem": (
                            "falha_tecnica"
                            if p.cpf in tecnicos
                            else "novo"
                        ),
                    }
                    for p in fila
                ],
                [
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

            print(
                f"API total:                    "
                f"{len(base_api)}"
            )

            print(
                f"Pendentes fotografados:       "
                f"{len(cpfs_pendentes)}"
            )

            print("-" * 72)

            print(
                f"Novos para consultar:         "
                f"{contagem['fila_NOVO']}"
            )

            print(
                f"Falhas técnicas para rever:   "
                f"{contagem['fila_FALHA_TECNICA']}"
            )

            print(
                f"Concluídos ainda Pendentes:   "
                f"{contagem['fila_CONCLUIDO_PENDENTE']}"
            )

            print(
                f"Dados insuficientes:          "
                f"{contagem['dados_insuficientes']}"
            )

            print(
                f"Movidos durante varredura:    "
                f"{contagem['movido_durante_varredura']}"
            )

            print(
                f"Pendentes fora da API:        "
                f"{contagem['sem_api']}"
            )

            print()
            print(
                f"FILA REAL:                    "
                f"{len(fila)}"
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
                "\nDigite S para começar a consultar "
                "essa fila. Qualquer outra tecla "
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