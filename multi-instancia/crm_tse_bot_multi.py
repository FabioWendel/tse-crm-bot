from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


TOTAL_OPERADORES = 4
PORTA_TSE_INICIAL = 9320
PASTA_DESTA_VERSAO = Path(__file__).resolve().parent
RAIZ_PROJETO = PASTA_DESTA_VERSAO.parent

# O motor continua sendo a versao normal. Apenas os recursos que nao podem ser
# compartilhados entre processos sao redirecionados para a pasta da instancia.
os.environ["TSE_NAVEGADOR"] = "brave"
sys.path.insert(0, str(RAIZ_PROJETO))
import crm_tse_bot as bot  # noqa: E402


def ler_instancia() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        raise SystemExit("Informe o operador: 0, 1, 2 ou 3.")
    numero = int(sys.argv[1])
    if not 0 <= numero < TOTAL_OPERADORES:
        raise SystemExit("Operador invalido. Use 0, 1, 2 ou 3.")
    return numero


def configurar_instancia(numero: int) -> Path:
    dados = PASTA_DESTA_VERSAO / "dados" / f"operador-{numero}"
    dados.mkdir(parents=True, exist_ok=True)

    bot.BASE_DIR = dados
    bot.PROFILE_DIR = dados / "perfil-crm"
    bot.TSE_PROFILE_DIR = dados / "perfil-tse-brave"
    bot.LOG_FILE = dados / "consultas.csv"
    bot.ERROR_LOG = dados / "bot_error.log"
    bot.DETAIL_LOG = dados / "execucao_detalhada.txt"
    bot.TSE_NAVEGADOR = "brave"
    bot.TSE_REMOTE_DEBUGGING_PORT = PORTA_TSE_INICIAL + numero

    def perguntar_configuracao() -> tuple[int, int, int, int, int]:
        print(f"Operador fixo desta janela: {numero} de {TOTAL_OPERADORES}.")
        print(f"Porta exclusiva do TSE: {bot.TSE_REMOTE_DEBUGGING_PORT}.")
        limite = bot.ler_inteiro(
            "Quantos candidatos no maximo nesta rodada? [Enter/0 = fatia inteira] ",
            minimo=0,
            maximo=100000,
            padrao=0,
        )
        print("Sem teto: vou ate o fim desta fatia." if limite == 0 else f"Teto desta rodada: {limite} CPF(s).")
        pausar_a_cada = bot.ler_inteiro(
            "Fazer uma pausa a cada quantas pessoas? [Enter = 10, minimo = 1] ",
            minimo=1,
            maximo=100000,
            padrao=10,
        )
        duracao = bot.ler_inteiro(
            "Quantos segundos deve durar cada pausa? [Enter = 10, minimo = 1] ",
            minimo=1,
            maximo=3600,
            padrao=10,
        )
        print(f"Pausa configurada: {duracao}s a cada {pausar_a_cada} pessoa(s).")
        return numero, TOTAL_OPERADORES, limite, pausar_a_cada, duracao

    bot.perguntar_operador = perguntar_configuracao
    return dados


def executar() -> int:
    numero = ler_instancia()
    dados = configurar_instancia(numero)
    print("=" * 64)
    print(f" CRM x TSE - MULTI-INSTANCIA - OPERADOR {numero}")
    print("=" * 64)
    print(f"Dados exclusivos desta janela: {dados}")
    print("Cada operador usa perfil, porta e relatorios separados.")
    print("Se aparecer CAPTCHA, a resolucao continua sendo manual.")
    print("=" * 64)
    print()

    try:
        if "--teste" in sys.argv[2:]:
            return bot.autoteste()
        bot.main()
        print("\nConcluido.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrompido por voce. O que ja foi salvo continua no CRM e nos relatorios.")
        return 130
    except Exception:
        with bot.ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"\n{'=' * 70}\n{datetime.now().isoformat(timespec='seconds')} | erro geral\n")
            arquivo.write(traceback.format_exc())
        print(f"\nDeu erro. Salvei o detalhe em: {bot.ERROR_LOG}")
        print("Ultimas linhas do erro:")
        print("\n".join(traceback.format_exc().splitlines()[-8:]))
        return 1
    finally:
        bot.encerrar_sessao_tse()


if __name__ == "__main__":
    raise SystemExit(executar())
