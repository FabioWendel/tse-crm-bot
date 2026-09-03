from __future__ import annotations

import csv
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright


TOTAL_OPERADORES = 4
PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent
PASTA_DADOS = PASTA_ATUAL / "dados"
PASTA_RODADA = RAIZ / "varredura-final-multi" / "dados"
FILA_ORIGINAL = RAIZ / "varredura-final" / "dados" / "fila_varredura.csv"

AUDITORIA_CSV = PASTA_DADOS / "auditoria_rodada.csv"
FILA_RECUPERACAO_CSV = PASTA_DADOS / "fila_recuperacao.csv"
INDETERMINADOS_CSV = PASTA_DADOS / "indeterminados.csv"

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


bot.BASE_DIR = PASTA_DADOS
bot.PROFILE_DIR = PASTA_DADOS / "perfil-crm"
bot.LOG_FILE = PASTA_DADOS / "consultas.csv"
bot.ERROR_LOG = PASTA_DADOS / "bot_error.log"
bot.DETAIL_LOG = PASTA_DADOS / "execucao_detalhada.txt"


PADRAO_ERRO = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"\s*\|\s*CPF\s+(\d{11})\s*\|\s*tentativa\s+\d+",
    re.IGNORECASE,
)
PADRAO_DETALHE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"\s*\|\s*(.*?)\s*\|\s*CPF\s+(\d{11})\s*$",
    re.IGNORECASE,
)
PADRAO_CONSULTANDO = re.compile(
    r"Consultando\s+(.+?)\s+-\s+CPF\s+(\d{11})",
    re.IGNORECASE,
)


@dataclass
class RegistroRodada:
    cpf: str
    nome: str = ""
    operadores: set[int] = field(default_factory=set)
    em_consultas_csv: bool = False
    em_bot_error: bool = False
    em_repasse: bool = False
    em_execucao_detalhada: bool = False
    inferido_por_faixa: bool = False
    suspeito_falso_skip: bool = False
    suspeito_dados_incompletos: bool = False
    ultimo_status: str = ""
    resultado: str = ""
    comunicado: str = ""
    data_hora: str = ""
    observacoes: list[str] = field(default_factory=list)


def obter(
    registros: dict[str, RegistroRodada],
    cpf: str,
    operador: int | None = None,
) -> RegistroRodada | None:
    cpf = bot.only_digits(cpf)
    if len(cpf) != 11:
        return None
    registro = registros.setdefault(cpf, RegistroRodada(cpf=cpf))
    if operador is not None:
        registro.operadores.add(operador)
    return registro


def atualizar_ultimo(
    registro: RegistroRodada,
    data_hora: str,
    status: str = "",
    resultado: str = "",
    comunicado: str = "",
) -> None:
    if registro.data_hora and data_hora and data_hora < registro.data_hora:
        return
    registro.data_hora = data_hora or registro.data_hora
    registro.ultimo_status = status or registro.ultimo_status
    registro.resultado = resultado or registro.resultado
    registro.comunicado = comunicado or registro.comunicado


def contem_dados_incompletos(*valores: str) -> bool:
    texto = bot.normalize_text(" | ".join(str(valor or "") for valor in valores))
    return any(
        termo in texto
        for termo in (
            "DADOS INCOMPLETOS NO CRM",
            "CONSULTA AO TSE NAO REALIZADA",
            "CAMPOS AUSENTES",
        )
    )


def contem_falso_skip(texto: str) -> bool:
    texto = bot.normalize_text(texto)
    return any(
        termo in texto
        for termo in (
            "ESSA PESSOA NAO ESTA MAIS EM PENDENTES",
            "OUTRO OPERADOR TRATOU",
            "PULANDO",
        )
    )


def ler_consultas(
    caminho: Path,
    operador: int,
    registros: dict[str, RegistroRodada],
) -> None:
    if not caminho.exists():
        return
    with caminho.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            registro = obter(registros, str(linha.get("cpf") or ""), operador)
            if registro is None:
                continue
            registro.em_consultas_csv = True
            registro.nome = str(linha.get("nome") or registro.nome).strip()
            status = str(linha.get("status") or "").strip()
            resultado = str(linha.get("resultado") or "").strip()
            comunicado = str(linha.get("comunicado") or "").strip()
            atualizar_ultimo(
                registro,
                str(linha.get("data_hora") or "").strip(),
                status,
                resultado,
                comunicado,
            )
            if contem_dados_incompletos(status, resultado, comunicado):
                registro.suspeito_dados_incompletos = True


def ler_bot_error(
    caminho: Path,
    operador: int,
    registros: dict[str, RegistroRodada],
) -> None:
    if not caminho.exists():
        return
    atual: RegistroRodada | None = None
    with caminho.open("r", encoding="utf-8", errors="replace") as arquivo:
        for linha in arquivo:
            cabecalho = PADRAO_ERRO.match(linha.strip())
            if cabecalho:
                atual = obter(registros, cabecalho.group(2), operador)
                if atual is not None:
                    atual.em_bot_error = True
                    atualizar_ultimo(atual, cabecalho.group(1))
                continue
            consulta = PADRAO_CONSULTANDO.search(linha)
            if consulta:
                atual = obter(registros, consulta.group(2), operador)
                if atual is not None and not atual.nome:
                    atual.nome = consulta.group(1).strip()
            if atual is not None and contem_falso_skip(linha):
                atual.suspeito_falso_skip = True


def ler_execucao_detalhada(
    caminho: Path,
    operador: int,
    registros: dict[str, RegistroRodada],
) -> None:
    if not caminho.exists():
        return
    atual: RegistroRodada | None = None
    with caminho.open("r", encoding="utf-8", errors="replace") as arquivo:
        for linha in arquivo:
            cabecalho = PADRAO_DETALHE.match(linha.strip())
            if cabecalho:
                atual = obter(registros, cabecalho.group(3), operador)
                if atual is not None:
                    atual.em_execucao_detalhada = True
                    atual.nome = cabecalho.group(2).strip() or atual.nome
                    atualizar_ultimo(atual, cabecalho.group(1))
                continue
            consulta = PADRAO_CONSULTANDO.search(linha)
            if consulta:
                atual = obter(registros, consulta.group(2), operador)
                if atual is not None and not atual.nome:
                    atual.nome = consulta.group(1).strip()
            if atual is None:
                continue
            if contem_falso_skip(linha):
                atual.suspeito_falso_skip = True
            if contem_dados_incompletos(linha):
                atual.suspeito_dados_incompletos = True


def ler_repasse(
    caminho: Path,
    operador: int,
    registros: dict[str, RegistroRodada],
) -> None:
    if not caminho.exists():
        return
    with caminho.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            registro = obter(registros, str(linha.get("cpf") or ""), operador)
            if registro is None:
                continue
            registro.em_repasse = True
            registro.nome = str(linha.get("nome") or registro.nome).strip()


def carregar_fila_original() -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    if not FILA_ORIGINAL.exists():
        raise RuntimeError(f"Fila original não encontrada: {FILA_ORIGINAL}")
    linhas: list[dict[str, str]] = []
    por_cpf: dict[str, dict[str, str]] = {}
    with FILA_ORIGINAL.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            cpf = bot.only_digits(str(linha.get("cpf") or ""))
            if len(cpf) != 11 or cpf in por_cpf:
                continue
            copia = dict(linha)
            copia["cpf"] = cpf
            linhas.append(copia)
            por_cpf[cpf] = copia
    return linhas, por_cpf


def incluir_faixas_percorridas(
    fila_original: list[dict[str, str]],
    registros: dict[str, RegistroRodada],
) -> None:
    for operador in range(TOTAL_OPERADORES):
        fatia = fila_original[operador::TOTAL_OPERADORES]
        posicoes = {linha["cpf"]: indice for indice, linha in enumerate(fatia)}
        posicoes_explicitas = [
            posicoes[cpf]
            for cpf, registro in registros.items()
            if operador in registro.operadores and cpf in posicoes
        ]
        if not posicoes_explicitas:
            continue
        ultimo_indice = max(posicoes_explicitas)
        for linha in fatia[: ultimo_indice + 1]:
            cpf = linha["cpf"]
            registro = obter(registros, cpf, operador)
            if registro is None:
                continue
            if not registro.nome:
                registro.nome = str(linha.get("nome") or "").strip()
            if not (
                registro.em_consultas_csv
                or registro.em_bot_error
                or registro.em_repasse
                or registro.em_execucao_detalhada
            ):
                registro.inferido_por_faixa = True
                registro.suspeito_falso_skip = True
                registro.observacoes.append(
                    "CPF inferido no trecho sequencial já percorrido pelo operador; "
                    "não havia registro explícito nos arquivos da rodada."
                )


def salvar_csv(
    caminho: Path,
    registros: list[dict[str, str]],
    campos: list[str],
) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=campos)
        writer.writeheader()
        writer.writerows(registros)


def motivo_recuperacao(registro: RegistroRodada) -> str:
    if registro.suspeito_falso_skip:
        return "falso_skip_suspeito"
    if registro.suspeito_dados_incompletos:
        return "dados_incompletos"
    if registro.em_repasse:
        return "repasse"
    if registro.em_bot_error:
        return "erro_tecnico"
    return "ainda_pendente_apos_consulta"


def historico_resumido(registro: RegistroRodada) -> str:
    fontes: list[str] = []
    if registro.em_consultas_csv:
        fontes.append("consultas.csv")
    if registro.em_bot_error:
        fontes.append("bot_error.log")
    if registro.em_repasse:
        fontes.append("repasse.csv")
    if registro.em_execucao_detalhada:
        fontes.append("execucao_detalhada.txt")
    if registro.inferido_por_faixa:
        fontes.append("faixa_sequencial_inferida")
    if registro.ultimo_status:
        fontes.append(f"status={registro.ultimo_status}")
    return " | ".join(fontes)


def executar() -> int:
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    fila_original, origem_por_cpf = carregar_fila_original()
    registros: dict[str, RegistroRodada] = {}

    for operador in range(TOTAL_OPERADORES):
        pasta = PASTA_RODADA / f"operador-{operador}"
        ler_consultas(pasta / "consultas.csv", operador, registros)
        ler_bot_error(pasta / "bot_error.log", operador, registros)
        ler_execucao_detalhada(pasta / "execucao_detalhada.txt", operador, registros)
        ler_repasse(pasta / "repasse.csv", operador, registros)

    incluir_faixas_percorridas(fila_original, registros)
    print(f"CPFs envolvidos ou inferidos na rodada: {len(registros)}")

    auditoria: list[dict[str, str]] = []
    fila_recuperacao: list[dict[str, str]] = []
    indeterminados: list[dict[str, str]] = []
    context = None

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
                raise RuntimeError("Não consegui abrir o CRM para a auditoria somente leitura.")
            bot.ensure_crm_ready(crm)

            for indice, cpf in enumerate(sorted(registros), start=1):
                registro = registros[cpf]
                origem = origem_por_cpf.get(cpf, {})
                confirmacao = bot.confirmar_pendente_por_api(crm, cpf)
                estado_api = confirmacao.estado.value
                if confirmacao.estado == bot.EstadoPendente.PENDENTE:
                    decisao = "RECUPERAR"
                    pessoa = confirmacao.pessoa
                    if pessoa is None:
                        estado_api = bot.EstadoPendente.INDETERMINADO.value
                        decisao = "NAO_DECIDIR"
                    else:
                        fila_recuperacao.append({
                            "id": pessoa.crm_id or origem.get("id", ""),
                            "cpf": cpf,
                            "nome": pessoa.nome or registro.nome or origem.get("nome", ""),
                            "mae": pessoa.mae or origem.get("mae", ""),
                            "nascimento": pessoa.nascimento or origem.get("nascimento", ""),
                            "operador_origem": ",".join(map(str, sorted(registro.operadores))),
                            "motivo_recuperacao": motivo_recuperacao(registro),
                            "historico": historico_resumido(registro),
                        })
                elif confirmacao.estado == bot.EstadoPendente.FORA_DE_PENDENTES:
                    decisao = "JA_ENCERRADO"
                else:
                    decisao = "NAO_DECIDIR"

                observacoes = list(registro.observacoes)
                if confirmacao.detalhe:
                    observacoes.append(confirmacao.detalhe)
                pessoa_api = confirmacao.pessoa
                linha_auditoria = {
                    "cpf": cpf,
                    "nome": (
                        (pessoa_api.nome if pessoa_api is not None else "")
                        or registro.nome
                        or str(origem.get("nome") or "")
                    ),
                    "operador": ",".join(map(str, sorted(registro.operadores))),
                    "em_consultas_csv": "sim" if registro.em_consultas_csv else "não",
                    "em_bot_error": "sim" if registro.em_bot_error else "não",
                    "em_repasse": "sim" if registro.em_repasse else "não",
                    "em_execucao_detalhada": (
                        "sim" if registro.em_execucao_detalhada else "não"
                    ),
                    "suspeito_falso_skip": "sim" if registro.suspeito_falso_skip else "não",
                    "suspeito_dados_incompletos": (
                        "sim" if registro.suspeito_dados_incompletos else "não"
                    ),
                    "ultimo_status": registro.ultimo_status,
                    "resultado": registro.resultado,
                    "comunicado": registro.comunicado,
                    "data_hora": registro.data_hora,
                    "estado_api": estado_api,
                    "decisao": decisao,
                    "observacao": " | ".join(observacoes),
                }
                auditoria.append(linha_auditoria)
                if decisao == "NAO_DECIDIR":
                    indeterminados.append(dict(linha_auditoria))
                print(
                    f"[{indice}/{len(registros)}] CPF {cpf}: "
                    f"{estado_api} => {decisao}"
                )
    except KeyboardInterrupt:
        print("\nAuditoria interrompida. Salvando os resultados já confirmados.")
    except Exception:
        with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(
                f"\n{'=' * 70}\n"
                f"{datetime.now().isoformat(timespec='seconds')} | erro da auditoria\n"
            )
            arquivo.write(traceback.format_exc())
        print("A auditoria encontrou um erro geral; os resultados parciais serão salvos.")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    campos_auditoria = [
        "cpf",
        "nome",
        "operador",
        "em_consultas_csv",
        "em_bot_error",
        "em_repasse",
        "em_execucao_detalhada",
        "suspeito_falso_skip",
        "suspeito_dados_incompletos",
        "ultimo_status",
        "resultado",
        "comunicado",
        "data_hora",
        "estado_api",
        "decisao",
        "observacao",
    ]
    salvar_csv(AUDITORIA_CSV, auditoria, campos_auditoria)
    salvar_csv(INDETERMINADOS_CSV, indeterminados, campos_auditoria)
    salvar_csv(
        FILA_RECUPERACAO_CSV,
        fila_recuperacao,
        [
            "id",
            "cpf",
            "nome",
            "mae",
            "nascimento",
            "operador_origem",
            "motivo_recuperacao",
            "historico",
        ],
    )

    print("=" * 72)
    print(f"Auditados: {len(auditoria)}")
    print(f"Recuperar: {len(fila_recuperacao)}")
    print(f"Indeterminados: {len(indeterminados)}")
    print(f"Auditoria: {AUDITORIA_CSV}")
    print(f"Fila de recuperação: {FILA_RECUPERACAO_CSV}")
    print(f"Indeterminados: {INDETERMINADOS_CSV}")
    print("Nenhuma consulta ao TSE ou alteração no CRM foi realizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(executar())
