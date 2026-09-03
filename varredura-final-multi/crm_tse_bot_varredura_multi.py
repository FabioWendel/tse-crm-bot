from __future__ import annotations

import csv
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright


TOTAL_OPERADORES = 4
PORTA_TSE_INICIAL = 9380
PASTA_ATUAL = Path(__file__).resolve().parent
RAIZ = PASTA_ATUAL.parent
FILA_VARREDURA = RAIZ / "varredura-final" / "dados" / "fila_varredura.csv"

os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ))

import crm_tse_bot as bot  # noqa: E402


def ler_operador() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit("Informe o operador: 0, 1, 2 ou 3.")
    operador = int(sys.argv[1])
    if not 0 <= operador < TOTAL_OPERADORES:
        raise SystemExit("Operador inválido. Use 0, 1, 2 ou 3.")
    return operador


def configurar_operador(operador: int) -> Path:
    dados = PASTA_ATUAL / "dados" / f"operador-{operador}"
    dados.mkdir(parents=True, exist_ok=True)

    bot.BASE_DIR = dados
    bot.PROFILE_DIR = dados / "perfil-crm"
    bot.TSE_PROFILE_DIR = dados / "perfil-tse-brave"
    bot.LOG_FILE = dados / "consultas.csv"
    bot.ERROR_LOG = dados / "bot_error.log"
    bot.DETAIL_LOG = dados / "execucao_detalhada.txt"
    bot.TSE_NAVEGADOR = "brave"
    bot.TSE_REMOTE_DEBUGGING_PORT = PORTA_TSE_INICIAL + operador
    return dados


def normalizar_nascimento(valor: str) -> str:
    texto = str(valor or "").strip()
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return ""


def carregar_fila() -> tuple[list[bot.Pessoa], int]:
    if not FILA_VARREDURA.exists():
        raise RuntimeError(
            f"Fila não encontrada: {FILA_VARREDURA}. "
            "Execute primeiro a varredura final apenas até gerar a auditoria."
        )

    pessoas: list[bot.Pessoa] = []
    cpfs_vistos: set[str] = set()
    ignorados = 0

    with FILA_VARREDURA.open("r", encoding="utf-8-sig", newline="") as arquivo:
        for linha in csv.DictReader(arquivo):
            cpf = bot.only_digits(str(linha.get("cpf") or ""))
            nome = str(linha.get("nome") or "").strip()
            mae = str(linha.get("mae") or "").strip()
            nascimento = normalizar_nascimento(str(linha.get("nascimento") or ""))
            try:
                crm_id = int(linha.get("id") or 0)
            except (TypeError, ValueError):
                crm_id = 0

            if (
                len(cpf) != 11
                or not nome
                or not mae
                or not nascimento
                or cpf in cpfs_vistos
            ):
                ignorados += 1
                continue

            cpfs_vistos.add(cpf)
            pessoas.append(
                bot.Pessoa(
                    row_index=-1,
                    nome=nome,
                    cpf=cpf,
                    mae=mae,
                    nascimento=nascimento,
                    crm_id=crm_id if crm_id > 0 else None,
                )
            )

    return pessoas, ignorados


def salvar_repasse(caminho: Path, falhas: list[bot.Pessoa]) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8-sig", newline="") as arquivo:
        writer = csv.DictWriter(
            arquivo,
            fieldnames=["id", "cpf", "nome", "mae", "nascimento"],
        )
        writer.writeheader()
        for pessoa in falhas:
            writer.writerow({
                "id": pessoa.crm_id or "",
                "cpf": pessoa.cpf,
                "nome": pessoa.nome,
                "mae": pessoa.mae,
                "nascimento": pessoa.nascimento,
            })


def perguntar_configuracao(tamanho_fatia: int) -> tuple[int, int, int]:
    limite = bot.ler_inteiro(
        "Quantos CPFs processar nesta fatia? [Enter/0 = todos] ",
        minimo=0,
        maximo=tamanho_fatia,
        padrao=0,
    )
    pausar_a_cada = bot.ler_inteiro(
        "Fazer pausa a cada quantas pessoas? [Enter = 10] ",
        minimo=1,
        maximo=100000,
        padrao=10,
    )
    segundos = bot.ler_inteiro(
        "Quantos segundos deve durar cada pausa? [Enter = 10] ",
        minimo=1,
        maximo=3600,
        padrao=10,
    )
    return limite, pausar_a_cada, segundos


def executar() -> int:
    operador = ler_operador()
    dados = configurar_operador(operador)
    repasse_csv = dados / "repasse.csv"
    fila_total, ignorados = carregar_fila()

    # Todas as instâncias leem a mesma fotografia. O índice intercalado produz
    # quatro fatias exclusivas e equilibradas enquanto o CSV não for regenerado.
    fila = fila_total[operador::TOTAL_OPERADORES]

    print("=" * 72)
    print(f" VARREDURA FINAL MULTI - OPERADOR {operador} DE {TOTAL_OPERADORES}")
    print("=" * 72)
    print(f"Fila de origem: {FILA_VARREDURA}")
    print(f"Total válido da fotografia: {len(fila_total)}")
    print(f"Linhas inválidas/duplicadas ignoradas: {ignorados}")
    print(f"Fatia deste operador: {len(fila)}")
    print(f"Porta exclusiva do TSE: {bot.TSE_REMOTE_DEBUGGING_PORT}")
    print(f"Dados exclusivos: {dados}")
    print("Não regenere fila_varredura.csv enquanto as quatro fatias estiverem rodando.")

    if not fila:
        salvar_repasse(repasse_csv, [])
        print("Esta fatia está vazia.")
        return 0

    limite, pausar_a_cada, segundos = perguntar_configuracao(len(fila))
    if limite:
        fila = fila[:limite]

    resposta = input(
        "Digite S para iniciar esta fatia. Qualquer outra tecla encerra: "
    ).strip().upper()
    if resposta != "S":
        print("Operador encerrado sem consultar o TSE.")
        return 0

    bot.TSE_PAUSAR_A_CADA_PESSOAS = pausar_a_cada
    bot.TSE_DURACAO_PAUSA_MS = segundos * 1000
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
                raise RuntimeError("Não consegui abrir o CRM.")
            bot.ensure_crm_ready(crm)

            print(
                "Antes de cada consulta, o motor confirma novamente "
                "se o CPF continua em Pendentes."
            )
            falhas = bot.rodar_fila(
                playwright,
                context,
                crm,
                fila,
                f"operador-{operador} ",
            )
            salvar_repasse(repasse_csv, falhas)

            if falhas and not bot._PERFIL_TSE_BLOQUEADO:
                print(f"\nRepasse do operador {operador}: {len(falhas)} pessoa(s).")
                falhas = bot.rodar_fila(
                    playwright,
                    context,
                    crm,
                    falhas,
                    f"repasse-{operador} ",
                )
                salvar_repasse(repasse_csv, falhas)

            print("=" * 72)
            print(f"Operador {operador} finalizado.")
            print(f"Fatia executada: {len(fila)}")
            print(f"Ainda no repasse: {len(falhas)}")
            print(f"Repasse salvo em: {repasse_csv}")
            return 0
    except KeyboardInterrupt:
        print("\nInterrompido. A fila original foi preservada para uma nova execução.")
        return 130
    except Exception:
        dados.mkdir(parents=True, exist_ok=True)
        with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(
                f"\n{'=' * 70}\n"
                f"{datetime.now().isoformat(timespec='seconds')} | erro geral\n"
            )
            arquivo.write(traceback.format_exc())
        print(f"\nErro no operador {operador}. Detalhes em: {bot.ERROR_LOG}")
        print("\n".join(traceback.format_exc().splitlines()[-8:]))
        return 1
    finally:
        bot.encerrar_sessao_tse()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(executar())
