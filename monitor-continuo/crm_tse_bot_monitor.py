from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


PENDENTES_API_URL = "https://juniorveloso.com.br/cadastrante/api/validar-local"
PENDENTES_POR_PAGINA = 100
MAX_PAGINAS_API = 1000
INTERVALO_PADRAO_SEGUNDOS = 30
BACKOFF_MAXIMO_SEGUNDOS = 300
LIMITE_QUARENTENA = 5
PORTA_TSE_MONITOR = 9370

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
}

PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent
PASTA_DADOS = PASTA_ATUAL / "dados"

REPASSE_CSV = PASTA_DADOS / "repasse.csv"
QUARENTENA_CSV = PASTA_DADOS / "quarentena.csv"
DADOS_INCOMPLETOS_CSV = PASTA_DADOS / "dados_incompletos.csv"

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


# O motor continua sendo crm_tse_bot.py. Somente perfis, porta e arquivos de
# execução são exclusivos deste modo.
bot.BASE_DIR = PASTA_DADOS
bot.PROFILE_DIR = PASTA_DADOS / "perfil-crm"
bot.TSE_PROFILE_DIR = PASTA_DADOS / "perfil-tse-brave"
bot.LOG_FILE = PASTA_DADOS / "consultas.csv"
bot.ERROR_LOG = PASTA_DADOS / "bot_error.log"
bot.DETAIL_LOG = PASTA_DADOS / "execucao_detalhada.txt"
bot.TSE_NAVEGADOR = "brave"
bot.TSE_REMOTE_DEBUGGING_PORT = PORTA_TSE_MONITOR


class FalhaGraveMonitor(RuntimeError):
    pass


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalizar(valor: object) -> str:
    return re.sub(r"\s+", " ", str(valor or "").strip().lower())


def campo_valido(valor: object) -> bool:
    return normalizar(valor) not in VALORES_SEM_DADO


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


def validar_dados(pessoa: bot.Pessoa) -> tuple[bool, str]:
    problemas: list[str] = []
    if not campo_valido(pessoa.nome):
        problemas.append("nome")
    if len(bot.only_digits(pessoa.cpf)) != 11:
        problemas.append("CPF")
    if not campo_valido(pessoa.mae):
        problemas.append("nome da mãe")
    if not normalizar_nascimento(pessoa.nascimento):
        problemas.append("nascimento")
    return not problemas, ", ".join(problemas)


def assinatura_dados(pessoa: bot.Pessoa) -> str:
    conteudo = "\x1f".join(
        (
            normalizar(pessoa.nome),
            bot.only_digits(pessoa.cpf),
            normalizar(pessoa.mae),
            normalizar(pessoa.nascimento),
        )
    )
    return hashlib.sha256(conteudo.encode("utf-8")).hexdigest()


def carregar_pendentes_api(
    page: Page,
) -> tuple[list[bot.Pessoa], list[dict[str, str]]]:
    por_id: dict[int, bot.Pessoa] = {}
    cpfs_vistos: set[str] = set()
    ids_pagina_anterior: tuple[str, ...] | None = None
    invalidos: list[dict[str, str]] = []

    for numero_pagina in range(1, MAX_PAGINAS_API + 1):
        resposta = None
        try:
            resposta = page.request.get(
                PENDENTES_API_URL,
                params={
                    "aba": "pendentes",
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
                    f"API de Pendentes respondeu HTTP {resposta.status} "
                    f"na página {numero_pagina}."
                )

            payload = resposta.json()
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
                items = payload["items"]
            else:
                raise RuntimeError(
                    f"JSON inesperado na página {numero_pagina} da API de Pendentes."
                )

            ids_pagina = tuple(
                str(item.get("id") if isinstance(item, dict) else "<inválido>")
                for item in items
            )
            if items and ids_pagina == ids_pagina_anterior:
                raise RuntimeError(
                    "A API repetiu exatamente os IDs da página anterior; "
                    "o ciclo foi interrompido com segurança."
                )
            ids_pagina_anterior = ids_pagina

            antes = len(por_id)
            for indice, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    invalidos.append({
                        "id": "",
                        "cpf": "",
                        "nome": "",
                        "mae": "",
                        "nascimento": "",
                        "motivo": (
                            f"Página {numero_pagina}, item {indice}: "
                            "item JSON inválido."
                        ),
                    })
                    continue

                try:
                    crm_id = int(item.get("id"))
                except (TypeError, ValueError):
                    crm_id = 0
                cpf = bot.only_digits(str(item.get("cpf") or ""))
                nome = str(item.get("nome") or "").strip()
                mae = str(item.get("nome_da_mae") or "").strip()
                nascimento_original = str(item.get("nascimento") or "").strip()

                problemas: list[str] = []
                if crm_id <= 0:
                    problemas.append("ID ausente ou inválido")
                if len(cpf) != 11:
                    problemas.append("CPF ausente ou inválido")
                if problemas:
                    invalidos.append({
                        "id": str(item.get("id") or ""),
                        "cpf": cpf,
                        "nome": nome,
                        "mae": mae,
                        "nascimento": nascimento_original,
                        "motivo": (
                            f"Página {numero_pagina}, item {indice}: "
                            + ", ".join(problemas)
                            + "."
                        ),
                    })
                    continue

                if crm_id in por_id or cpf in cpfs_vistos:
                    continue

                pessoa = bot.Pessoa(
                    row_index=-1,
                    nome=nome,
                    cpf=cpf,
                    mae=mae,
                    nascimento=normalizar_nascimento(nascimento_original),
                    crm_id=crm_id,
                )
                por_id[crm_id] = pessoa
                cpfs_vistos.add(cpf)

            print(
                f"API Pendentes: página {numero_pagina}, "
                f"+{len(por_id) - antes}, total {len(por_id)}"
            )

            if len(items) < PENDENTES_POR_PAGINA:
                return list(por_id.values()), invalidos
            if numero_pagina == MAX_PAGINAS_API:
                raise RuntimeError(
                    f"Limite de segurança de {MAX_PAGINAS_API} páginas atingido."
                )
        finally:
            if resposta is not None:
                resposta.dispose()

    return list(por_id.values()), invalidos


def ler_mapa_csv(caminho: Path, chave: str = "cpf") -> dict[str, dict[str, str]]:
    if not caminho.exists():
        return {}
    registros: dict[str, dict[str, str]] = {}
    with caminho.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            valor = bot.only_digits(str(linha.get(chave) or ""))
            if len(valor) == 11:
                registros[valor] = dict(linha)
    return registros


def salvar_csv_atomico(
    caminho: Path,
    registros: list[dict[str, str]],
    campos: list[str],
) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    with temporario.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=campos)
        writer.writeheader()
        writer.writerows(registros)
    os.replace(temporario, caminho)


def salvar_repasse(repasse: dict[str, dict[str, str]]) -> None:
    salvar_csv_atomico(
        REPASSE_CSV,
        [repasse[cpf] for cpf in sorted(repasse)],
        ["id", "cpf", "nome", "tentativas", "ultimo_erro", "ultima_tentativa"],
    )


def salvar_quarentena(quarentena: dict[str, dict[str, str]]) -> None:
    salvar_csv_atomico(
        QUARENTENA_CSV,
        [quarentena[cpf] for cpf in sorted(quarentena)],
        [
            "cpf",
            "nome",
            "tentativas",
            "ultimo_erro",
            "data_hora",
            "saiu_de_pendentes",
        ],
    )


def salvar_dados_incompletos(
    incompletos: dict[str, dict[str, str]],
    invalidos_api: list[dict[str, str]],
) -> None:
    registros = [incompletos[cpf] for cpf in sorted(incompletos)]
    for item in invalidos_api:
        registros.append({
            "id": item["id"],
            "cpf": item["cpf"],
            "nome": item["nome"],
            "mae": item["mae"],
            "nascimento": item["nascimento"],
            "motivo": item["motivo"],
            "assinatura": "",
            "data_hora": agora(),
        })
    salvar_csv_atomico(
        DADOS_INCOMPLETOS_CSV,
        registros,
        [
            "id",
            "cpf",
            "nome",
            "mae",
            "nascimento",
            "motivo",
            "assinatura",
            "data_hora",
        ],
    )


def tentativas_registro(registro: dict[str, str]) -> int:
    try:
        return max(0, int(registro.get("tentativas") or 0))
    except (TypeError, ValueError):
        return 0


def registro_repasse(
    pessoa: bot.Pessoa,
    tentativas: int,
    ultimo_erro: str,
) -> dict[str, str]:
    return {
        "id": str(pessoa.crm_id or ""),
        "cpf": pessoa.cpf,
        "nome": pessoa.nome,
        "tentativas": str(tentativas),
        "ultimo_erro": ultimo_erro,
        "ultima_tentativa": agora(),
    }


def reconciliar_quarentena(
    quarentena: dict[str, dict[str, str]],
    pendentes_por_cpf: dict[str, bot.Pessoa],
) -> None:
    for cpf in list(quarentena):
        registro = quarentena[cpf]
        if cpf not in pendentes_por_cpf:
            registro["saiu_de_pendentes"] = "sim"
        elif normalizar(registro.get("saiu_de_pendentes")) == "sim":
            print(f"CPF {cpf} saiu e voltou a Pendentes; quarentena liberada.")
            del quarentena[cpf]


def preparar_pendentes_validos(
    pendentes: list[bot.Pessoa],
    incompletos: dict[str, dict[str, str]],
) -> dict[str, bot.Pessoa]:
    pendentes_por_cpf = {pessoa.cpf: pessoa for pessoa in pendentes}
    for cpf in list(incompletos):
        if cpf not in pendentes_por_cpf:
            del incompletos[cpf]

    validos: dict[str, bot.Pessoa] = {}
    for cpf, pessoa in pendentes_por_cpf.items():
        assinatura = assinatura_dados(pessoa)
        anterior = incompletos.get(cpf)
        if anterior and anterior.get("assinatura") == assinatura:
            continue

        valido, motivo = validar_dados(pessoa)
        if valido:
            incompletos.pop(cpf, None)
            validos[cpf] = pessoa
            continue

        incompletos[cpf] = {
            "id": str(pessoa.crm_id or ""),
            "cpf": cpf,
            "nome": pessoa.nome,
            "mae": pessoa.mae,
            "nascimento": pessoa.nascimento,
            "motivo": f"Campos ausentes ou inválidos: {motivo}.",
            "assinatura": assinatura,
            "data_hora": agora(),
        }

    return validos


def remover_estados_encerrados(
    repasse: dict[str, dict[str, str]],
    pendentes_por_cpf: dict[str, bot.Pessoa],
) -> None:
    for cpf in list(repasse):
        if cpf not in pendentes_por_cpf:
            del repasse[cpf]


def mover_falha_para_estado(
    pessoa: bot.Pessoa,
    repasse: dict[str, dict[str, str]],
    quarentena: dict[str, dict[str, str]],
    erro: str,
) -> None:
    anterior = repasse.get(pessoa.cpf, {})
    tentativas = tentativas_registro(anterior) + 1
    if tentativas >= LIMITE_QUARENTENA:
        quarentena[pessoa.cpf] = {
            "cpf": pessoa.cpf,
            "nome": pessoa.nome,
            "tentativas": str(tentativas),
            "ultimo_erro": erro,
            "data_hora": agora(),
            "saiu_de_pendentes": "não",
        }
        repasse.pop(pessoa.cpf, None)
        print(f"CPF {pessoa.cpf} foi movido para quarentena após {tentativas} falhas.")
        return
    repasse[pessoa.cpf] = registro_repasse(pessoa, tentativas, erro)


def verificar_navegadores(crm: Page) -> None:
    if crm.is_closed():
        raise FalhaGraveMonitor("A página do CRM foi fechada.")
    if bot._PERFIL_TSE_BLOQUEADO:
        raise FalhaGraveMonitor(
            "O perfil do TSE ficou bloqueado com eleitor anterior; pausa segura necessária."
        )


def processar_lote(
    playwright,
    context,
    crm: Page,
    fila: list[bot.Pessoa],
    prefixo: str,
    repasse: dict[str, dict[str, str]],
    quarentena: dict[str, dict[str, str]],
) -> tuple[int, int]:
    if not fila:
        return 0, 0

    estados_anteriores = {
        pessoa.cpf: dict(repasse.get(pessoa.cpf, {}))
        for pessoa in fila
    }

    # Salvar antes protege o lote se houver Ctrl+C. Os sucessos serão retirados
    # depois; quem já saiu de Pendentes também será removido no próximo ciclo.
    for pessoa in fila:
        repasse.setdefault(
            pessoa.cpf,
            registro_repasse(pessoa, 0, "aguardando processamento"),
        )
    salvar_repasse(repasse)

    falhas = bot.rodar_fila(playwright, context, crm, fila, prefixo)
    verificar_navegadores(crm)
    falhas_por_cpf = {pessoa.cpf: pessoa for pessoa in falhas}

    for pessoa in fila:
        repasse.pop(pessoa.cpf, None)
    for pessoa in falhas_por_cpf.values():
        if estados_anteriores.get(pessoa.cpf):
            repasse[pessoa.cpf] = estados_anteriores[pessoa.cpf]
        mover_falha_para_estado(
            pessoa,
            repasse,
            quarentena,
            "falha técnica devolvida pelo motor normal",
        )

    salvar_repasse(repasse)
    salvar_quarentena(quarentena)
    return len(fila), len(falhas_por_cpf)


def executar_ciclo(
    playwright,
    context,
    crm: Page,
    repasse: dict[str, dict[str, str]],
    quarentena: dict[str, dict[str, str]],
    incompletos: dict[str, dict[str, str]],
) -> tuple[int, int]:
    print(f"\n{'=' * 72}")
    print(f"Novo ciclo: {agora()}")
    pendentes, invalidos_api = carregar_pendentes_api(crm)
    pendentes_por_cpf = {pessoa.cpf: pessoa for pessoa in pendentes}

    remover_estados_encerrados(repasse, pendentes_por_cpf)
    reconciliar_quarentena(quarentena, pendentes_por_cpf)
    validos = preparar_pendentes_validos(pendentes, incompletos)
    salvar_dados_incompletos(incompletos, invalidos_api)
    salvar_repasse(repasse)
    salvar_quarentena(quarentena)

    # Somente o repasse que já existia no começo do ciclo é processado agora.
    # Falhas novas ficam para o próximo ciclo, evitando duas consultas ao mesmo
    # CPF dentro de um único ciclo.
    cpfs_repasse_inicio = set(repasse)
    fila_repasse = [
        validos[cpf]
        for cpf in sorted(cpfs_repasse_inicio)
        if cpf in validos and cpf not in quarentena
    ]
    fila_nova = [
        pessoa
        for cpf, pessoa in validos.items()
        if cpf not in cpfs_repasse_inicio and cpf not in quarentena
    ]

    print(f"Pendentes atuais: {len(pendentes)}")
    print(f"Dados incompletos: {len(incompletos) + len(invalidos_api)}")
    print(f"Fila nova: {len(fila_nova)}")
    print(f"Repasse deste ciclo: {len(fila_repasse)}")
    print(f"Quarentena: {len(quarentena)}")

    processados = 0
    falhas = 0
    total, erros = processar_lote(
        playwright,
        context,
        crm,
        fila_nova,
        "monitor ",
        repasse,
        quarentena,
    )
    processados += total
    falhas += erros

    total, erros = processar_lote(
        playwright,
        context,
        crm,
        fila_repasse,
        "repasse-monitor ",
        repasse,
        quarentena,
    )
    processados += total
    falhas += erros

    print(
        f"Ciclo concluído: {processados} processado(s), "
        f"{falhas} falha(s), {len(repasse)} em repasse."
    )
    return processados, falhas


def registrar_erro_geral(mensagem: str) -> None:
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
        arquivo.write(f"\n{'=' * 70}\n{agora()} | {mensagem}\n")
        arquivo.write(traceback.format_exc())


def aguardar(segundos: int) -> None:
    print(f"Próximo ciclo em {segundos} segundo(s). Pressione Ctrl+C para encerrar.")
    for _ in range(segundos):
        time.sleep(1)


def executar() -> int:
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    repasse = ler_mapa_csv(REPASSE_CSV)
    quarentena = ler_mapa_csv(QUARENTENA_CSV)
    incompletos = ler_mapa_csv(DADOS_INCOMPLETOS_CSV)
    intervalo = bot.ler_inteiro(
        "Intervalo entre ciclos em segundos [Enter = 30] ",
        minimo=5,
        maximo=3600,
        padrao=INTERVALO_PADRAO_SEGUNDOS,
    )
    context = None

    print("=" * 72)
    print(" MONITOR CONTÍNUO CRM x TSE")
    print("=" * 72)
    print(f"Porta exclusiva do TSE: {bot.TSE_REMOTE_DEBUGGING_PORT}")

    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(bot.PROFILE_DIR),
                executable_path=bot.find_chrome_executable(),
                headless=bot.HEADLESS,
                slow_mo=bot.SLOW_MO_MS,
                viewport={"width": 1366, "height": 768},
                args=["--start-maximized"],
            )
            crm = context.pages[0] if context.pages else context.new_page()
            if not bot.ir_para(crm, bot.CRM_URL):
                raise FalhaGraveMonitor("Não consegui abrir o CRM.")
            bot.ensure_crm_ready(crm)

            falhas_gerais = 0
            indisponibilidade_tse = 0
            while True:
                try:
                    processados, falhas = executar_ciclo(
                        playwright,
                        context,
                        crm,
                        repasse,
                        quarentena,
                        incompletos,
                    )
                    falhas_gerais = 0
                    if processados >= 3 and falhas == processados:
                        indisponibilidade_tse += 1
                    else:
                        indisponibilidade_tse = 0
                    espera = intervalo
                    if indisponibilidade_tse:
                        espera = max(
                            espera,
                            min(
                                30 * (2 ** (indisponibilidade_tse - 1)),
                                BACKOFF_MAXIMO_SEGUNDOS,
                            ),
                        )
                    aguardar(espera)
                except FalhaGraveMonitor:
                    raise
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    falhas_gerais += 1
                    registrar_erro_geral(f"erro temporário do ciclo: {exc}")
                    espera = min(
                        30 * (2 ** (falhas_gerais - 1)),
                        BACKOFF_MAXIMO_SEGUNDOS,
                    )
                    print(f"Erro temporário: {exc}")
                    aguardar(espera)
    except KeyboardInterrupt:
        print("\nCtrl+C recebido. Salvando o estado e encerrando com segurança.")
        return 0
    except FalhaGraveMonitor as exc:
        registrar_erro_geral(f"falha grave do monitor: {exc}")
        print(f"\nFalha grave: {exc}")
        return 1
    except Exception as exc:
        registrar_erro_geral(f"falha geral do monitor: {exc}")
        print(f"\nFalha geral: {exc}")
        return 1
    finally:
        salvar_repasse(repasse)
        salvar_quarentena(quarentena)
        bot.encerrar_sessao_tse()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(executar())
