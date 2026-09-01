from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from playwright.sync_api import BrowserContext, Error, Page, TimeoutError, sync_playwright


CRM_URL = "https://juniorveloso.com.br/cadastrante/validar-local"
TSE_URL = "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral#/"


def pasta_base() -> Path:
    """Pasta onde ficam CSV, log e perfis do navegador.

    Empacotado com PyInstaller, __file__ aponta para a extracao temporaria
    (_MEIxxxx), que e apagada ao sair: o CSV desapareceria a cada execucao.
    Congelado, ancora ao lado do .exe; em desenvolvimento, ao lado do .py.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = pasta_base()
TSE_NAVEGADOR = os.environ.get("TSE_NAVEGADOR", "chrome").strip().lower()
PROFILE_DIR = BASE_DIR / ".browser-profile"
TSE_PROFILE_DIR = BASE_DIR / (".tse-brave-profile" if TSE_NAVEGADOR == "brave" else ".tse-chrome-profile")
LOG_FILE = BASE_DIR / "consultas.csv"
ERROR_LOG = BASE_DIR / "bot_error.log"
DETAIL_LOG = BASE_DIR / "execucao_detalhada.txt"
LOG_HEADER = ["data_hora", "nome", "cpf", "mae", "nascimento", "encontrado", "irregular", "status", "comunicado", "resultado"]
CHROME_EXECUTABLE = ""
TSE_REMOTE_DEBUGGING_PORT = 9226 if TSE_NAVEGADOR == "brave" else 9222

HEADLESS = False
SLOW_MO_MS = 120
LIMITE_PADRAO = 50  # teto sugerido de CPFs por operador em cada rodada
MAX_PAGINAS_INVENTARIO = 1000  # cobre bases grandes sem remover a trava contra loop infinito
CRM_ITENS_POR_PAGINA = 50  # reduz DOM e memoria do renderer do CRM
CRM_RECARREGAR_A_CADA_PESSOAS = 50
CRM_DIVISAO_FIXA_QUATRO = False
TENTATIVAS_POR_PESSOA = 3  # tentativas reservadas ao inventario inicial do CRM
TENTATIVAS_PROCESSAMENTO_POR_PESSOA = 1  # falha inesperada segue para o repasse final
PAUSA_ENTRE_TENTATIVAS_MS = 3000
TSE_PAUSAR_A_CADA_PESSOAS = 10  # valores iniciais; operador escolhe no inicio
TSE_DURACAO_PAUSA_MS = 10000
TSE_RESPONSE_TIMEOUT_MS = 90000  # espera normal, sem CAPTCHA na tela
TSE_ESPERA_CAPTCHA_MS = 180000  # prorrogacao enquanto houver CAPTCHA (voce resolvendo)
TSE_ESPERA_MAXIMA_MS = 240000  # teto por tentativa: 4 min e reinicia a consulta
TEMPO_MAXIMO_POR_PESSOA_MS = 600000  # teto por pessoa: 10 min e vai para a proxima
MAX_TSE_ATTEMPTS = 3
ENTRADA_MANUAL_TIMEOUT_S = 120  # perguntas durante uma pessoa nunca param a fila indefinidamente

# False = o programa nunca para para voce digitar no terminal. O CAPTCHA
# continua sendo resolvido por voce no navegador; o que sai e a espera por
# Enter. True volta ao fluxo antigo, com confirmacoes e colagem manual.
PERGUNTAR_NO_TERMINAL = False
ESPERA_LOGIN_MS = 600000  # 10 min para o operador logar no CRM, sem pedir Enter
TSE_NO_CHROME_NORMAL = True  # usa Chrome separado, preenche a consulta e deixa CAPTCHA manual

# Quando o TSE responde que nao localizou o eleitor, o bot clica "Nao achei" na linha do CRM.
# Ja quando a consulta nem chegou a acontecer (CAPTCHA indisponivel, tela travada, timeout),
# o padrao e NAO marcar nada: falha nossa nao e ausencia de cadastro no TSE.
# Ponha True se preferir marcar "Nao achei" tambem nesses casos.
MARCAR_NAO_ACHEI_EM_ERRO_TECNICO = False

# Pessoa com situacao irregular (cancelado, suspenso, biometria etc.) mas COM local
# de votacao: False salva no CRM direto, True volta a pedir confirmacao no terminal.
# O caso irregular continua sempre visivel no terminal e na coluna 'irregular' do CSV.
CONFIRMAR_IRREGULAR = False

IRREGULAR_TERMS = (
    "CANCELADO",
    "CANCELADA",
    "SUSPENSO",
    "SUSPENSA",
    "INVALIDO",
    "INVÁLIDO",
    "INEXISTENTE",
    "IMPED",
    "NAO QUITE",
    "NÃO QUITE",
    "REVISAO DE ELEITORADO",
    "REVISÃO DE ELEITORADO",
)

# Rotulos alternativos aceitos para cada motivo de inativacao, em ordem de preferencia.
# Servem quando o texto do select do CRM nao bate exatamente com o motivo calculado.
MOTIVO_ALTERNATIVAS = {
    "Não quite": (
        "Não quite com a Justiça Eleitoral",
        "Não quite com a justiça eleitoral",
        "Quitação eleitoral pendente",
        "Título irregular",
    ),
    "Título cancelado": ("Título cancelado/suspenso", "Título suspenso", "Cancelado"),
    "Problema na biometria": ("Biometria não coletada", "Sem biometria"),
    "Dados inválidos": ("Dados incorretos", "Dados não conferem"),
}

# Opcoes "guarda-chuva" do select, usadas so quando nenhuma especifica casa.
MOTIVO_GENERICO = ("OUTRO", "OUTROS", "OUTRO MOTIVO", "OUTROS MOTIVOS")

CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".g-recaptcha",
    ".h-captcha",
    "text=/captcha|robô|robo|validação/i",
)

CAPTCHA_ERROR_TERMS = (
    "CAPTCHA INVALIDO",
    "CAPTCHA INVÁLIDO",
    "CAPTCHA ESTA INDISPONIVEL",
    "CAPTCHA ESTÁ INDISPONÍVEL",
    "SERVICO DE VERIFICACAO",
    "SERVIÇO DE VERIFICAÇÃO",
)

AUTH_RETRY_ERROR_TERMS = (
    "AUTENTICACAO REALIZADA MAS NIVEL DE ACESSO OBTIDO E MENOR",
    "NIVEL DE ACESSO OBTIDO E MENOR DO QUE O EXIGIDO",
)


@dataclass
class Pessoa:
    row_index: int
    nome: str
    cpf: str
    mae: str
    nascimento: str


@dataclass
class ResultadoTse:
    texto_para_crm: str
    status: str
    comunicado: str
    irregular: bool
    encontrado: bool
    precisa_tentar_de_novo: bool = False
    fechar_tse: Callable[[], None] | None = None
    # True quando o TSE de fato respondeu (achando ou negando). False quando a
    # consulta nao chegou a completar por CAPTCHA/timeout/tela travada.
    resposta_do_tse: bool = True


_TSE_BROWSER = None
_TSE_PROCESS = None
_TSE_CONTEXT = None
_PERFIL_TSE_BLOQUEADO = False
_PESSOAS_DESDE_ULTIMA_PAUSA = 0
_PESSOAS_DESDE_RENOVACAO_CRM = 0


def main() -> None:
    global TSE_PAUSAR_A_CADA_PESSOAS, TSE_DURACAO_PAUSA_MS
    numero, total_operadores, limite, pausar_a_cada, duracao_pausa_segundos = perguntar_operador()
    TSE_PAUSAR_A_CADA_PESSOAS = pausar_a_cada
    TSE_DURACAO_PAUSA_MS = duracao_pausa_segundos * 1000

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            executable_path=find_chrome_executable(),
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
            viewport={"width": 1366, "height": 768},
            args=["--start-maximized"],
        )

        crm = context.pages[0] if context.pages else context.new_page()
        if not ir_para(crm, CRM_URL):
            print("Nao consegui abrir o CRM. Confira sua internet e tente de novo.")
            context.close()
            return
        ensure_crm_ready(crm)

        todos = inventariar_com_retentativa(crm)
        if not todos:
            print("Nao encontrei linhas na tabela do CRM.")
            context.close()
            return

        fila = [p for p in todos if fatia_do_cpf(p.cpf, total_operadores) == numero]
        print(f"\nInventario: {len(todos)} pendente(s) no total.")
        print(f"Operador {numero} de {total_operadores}: {len(fila)} pessoa(s) na sua fatia.")

        if limite > 0 and len(fila) > limite:
            restantes = len(fila) - limite
            fila = fila[:limite]
            print(f"Teto de {limite}: vou processar {limite} agora e deixar {restantes} para a proxima rodada.")

        if not fila:
            print("Nenhuma pessoa caiu na sua fatia. Nada a fazer.")
            context.close()
            return

        falhas = rodar_fila(playwright, context, crm, fila, "")

        # Repasse: erro costuma ser transitorio (CRM lento, navegacao cortada,
        # TSE fora do ar por um instante). Vale uma segunda passada antes de
        # desistir de vez.
        if falhas and not _PERFIL_TSE_BLOQUEADO:
            print(f"\n{'=' * 60}")
            print(f"{len(falhas)} pessoa(s) falharam. Vou repassar essas agora.")
            print("=" * 60)
            falhas = rodar_fila(playwright, context, crm, falhas, "repasse ")
        elif falhas and _PERFIL_TSE_BLOQUEADO:
            print("\nRepasse cancelado: o perfil anterior continua ativo no TSE.")

        print(f"\n{'=' * 60}")
        print(f"Fim. Processadas: {len(fila) - len(falhas)}/{len(fila)}.")
        if falhas:
            print(f"Sem sucesso mesmo apos repasse: {len(falhas)}")
            for pendente in falhas:
                print(f"  - {pendente.nome} (CPF {pendente.cpf})")
            print(f"Detalhe dos erros em: {ERROR_LOG}")
            print("Rode o programa de novo mais tarde: quem ficou continua em Pendentes.")
        print("=" * 60)

        print("\nProcesso finalizado para as linhas visiveis.")
        context.close()


def ensure_crm_ready(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"Atualizar", re.I)).first.wait_for(timeout=8000)
        return
    except TimeoutError:
        pass

    print("\n>>> O CRM ainda nao mostrou a tabela.")
    print(">>> Se estiver na tela de login, faca login na janela do navegador.")
    print(">>> Nao precisa voltar aqui: eu sigo sozinho assim que a tabela aparecer.\n")

    if PERGUNTAR_NO_TERMINAL:
        ask_user_timeout("Depois que a tabela aparecer, pressione Enter aqui...", ENTRADA_MANUAL_TIMEOUT_S)
        ir_para(page, CRM_URL)

    # Espera a tabela por si mesma, em vez de exigir um Enter.
    limite = time.monotonic() + (ESPERA_LOGIN_MS / 1000)
    while time.monotonic() < limite:
        try:
            page.get_by_role("button", name=re.compile(r"Atualizar", re.I)).first.wait_for(timeout=5000)
            print("Tabela detectada. Seguindo.")
            return
        except (TimeoutError, Error):
            restante = int(limite - time.monotonic())
            print(f"  aguardando login... ({restante}s restantes)")

    print("Segui mesmo sem ver a tabela. Se a tela nao estiver certa, feche e abra de novo.")


def total_rows(page: Page) -> int:
    return page.locator("table tbody tr").count()


def aguardar_intervalo_entre_consultas(page: Page, segundos: int) -> None:
    """Mostra a contagem regressiva configurada pelo operador."""
    for restante in range(segundos, 0, -1):
        sys.stdout.write(f"\rProxima consulta em {restante:>4} segundo(s)...")
        sys.stdout.flush()
        try:
            page.wait_for_timeout(1000)
        except Error:
            time.sleep(1)
    sys.stdout.write("\rPausa concluida. Iniciando a proxima consulta.       \n")
    sys.stdout.flush()


def rodar_fila(playwright, context: BrowserContext, crm: Page, fila: list[Pessoa], prefixo: str) -> list[Pessoa]:
    """Processa a fila e devolve quem NAO deu certo.

    Erro numa pessoa nunca derruba as outras: cada uma e isolada, tentada mais
    de uma vez, e o que sobra volta na lista de falhas.
    """
    global _PERFIL_TSE_BLOQUEADO, _PESSOAS_DESDE_ULTIMA_PAUSA, _PESSOAS_DESDE_RENOVACAO_CRM
    falhas: list[Pessoa] = []
    for index, pendente in enumerate(fila, start=1):
        if _PESSOAS_DESDE_RENOVACAO_CRM >= CRM_RECARREGAR_A_CADA_PESSOAS:
            if not renovar_crm_controlado(crm):
                print("O CRM nao voltou apos a renovacao. Encerrando com seguranca e preservando o restante.")
                falhas.extend(fila[index - 1:])
                break
            _PESSOAS_DESDE_RENOVACAO_CRM = 0

        if _PESSOAS_DESDE_ULTIMA_PAUSA >= TSE_PAUSAR_A_CADA_PESSOAS:
            segundos = TSE_DURACAO_PAUSA_MS // 1000
            print(f"\nLote de {TSE_PAUSAR_A_CADA_PESSOAS} pessoa(s) concluido.")
            aguardar_intervalo_entre_consultas(crm, segundos)
            _PESSOAS_DESDE_ULTIMA_PAUSA = 0

        print(f"\n[{prefixo}{index}/{len(fila)}] Consultando {pendente.nome} - CPF {pendente.cpf}")
        try:
            if not processar_pessoa(playwright, context, crm, pendente):
                falhas.append(pendente)
        except PerfilTsePreso:
            _PERFIL_TSE_BLOQUEADO = True
            print("\nO TSE manteve o eleitor anterior autenticado. Encerrando a rodada para nao misturar consultas.")
            falhas.extend(fila[index - 1:])
            break
        except NavegadorMorto:
            # Sem navegador nao da para continuar; devolve o resto como pendente.
            print("\nO navegador foi fechado. Encerrando.")
            falhas.extend(fila[index - 1:])
            break
        _PESSOAS_DESDE_ULTIMA_PAUSA += 1
        _PESSOAS_DESDE_RENOVACAO_CRM += 1
    return falhas


class NavegadorMorto(RuntimeError):
    """Contexto/navegador caiu: nao adianta tentar de novo."""


class PerfilTsePreso(RuntimeError):
    """O TSE nao encerrou o eleitor anterior; continuar seria inseguro."""


def processar_pessoa(playwright, context: BrowserContext, crm: Page, pendente: Pessoa) -> bool:
    """Tenta tratar uma pessoa. True = resolvida (ou legitimamente pulada)."""
    for tentativa in range(1, TENTATIVAS_PROCESSAMENTO_POR_PESSOA + 1):
        try:
            return tratar_pessoa(playwright, context, crm, pendente)
        except KeyboardInterrupt:
            raise
        except PerfilTsePreso:
            raise
        except Exception as exc:
            if navegador_caiu(exc):
                raise NavegadorMorto() from exc

            registrar_erro(pendente, tentativa, exc)
            print(f"  ERRO (tentativa {tentativa}/{TENTATIVAS_PROCESSAMENTO_POR_PESSOA}): {resumo_erro(exc)}")

            if tentativa == TENTATIVAS_PROCESSAMENTO_POR_PESSOA:
                print("  Desisti desta pessoa por enquanto. Ela continua em Pendentes.")
                return False

            print("  Recuperando a tela do CRM para tentar de novo...")
            if not recuperar_crm(crm):
                print("  Nao consegui recuperar o CRM agora.")
                return False
    return False


def tratar_pessoa(playwright, context: BrowserContext, crm: Page, pendente: Pessoa) -> bool:
    # Rele a linha agora: entre o inventario e este momento outro operador
    # pode ter tratado a pessoa, e os dados podem ter mudado.
    pessoa = abrir_pessoa_por_cpf(crm, pendente)
    if not pessoa:
        print("Essa pessoa nao esta mais em Pendentes (outro operador tratou?). Pulando.")
        return True

    campos_ausentes = []
    if campo_sem_informacao(pessoa.mae):
        campos_ausentes.append("nome da mae")
    if campo_sem_informacao(pessoa.nascimento):
        campos_ausentes.append("data de nascimento")

    if campos_ausentes:
        descricao = " e ".join(campos_ausentes)
        print(f"Dados obrigatorios ausentes ({descricao}). Nao vou abrir nem consultar o TSE.")
        resultado = ResultadoTse(
            texto_para_crm=f"Consulta ao TSE nao realizada: {descricao} nao informado(s) no CRM.",
            status="Dados incompletos no CRM",
            comunicado=f"Campos ausentes: {descricao}.",
            irregular=False,
            encontrado=False,
            resposta_do_tse=False,
        )
        append_log(pessoa, resultado)
        if marcar_nao_achei(crm, pessoa):
            return True
        print("Nao confirmei a acao 'Nao achei'. A pessoa continua pendente.")
        return False

    resultado = consultar_tse(playwright, context, pessoa)
    try:
        append_log(pessoa, resultado)

        if not resultado.encontrado:
            if resultado.resposta_do_tse or MARCAR_NAO_ACHEI_EM_ERRO_TECNICO:
                print("TSE nao devolveu local de votacao. Vou marcar 'Nao achei' no CRM.")
                if marcar_nao_achei(crm, pessoa):
                    return True
                print("Nao confirmei a acao 'Nao achei'. A pessoa continua pendente.")
                return False

            # A consulta nem completou: nao e "nao achei", e falha nossa.
            # Devolve False para a pessoa entrar no repasse do fim da fila.
            print("A consulta nao chegou a completar (CAPTCHA/timeout). Fica para o repasse.")
            return False

        if resultado.irregular:
            print("\nATENCAO: situacao/comunicado irregular (registrado no CSV).")
            print(f"Pessoa: {pessoa.nome} - CPF {pessoa.cpf}")
            print(f"Status: {resultado.status or 'nao identificado'}")
            if resultado.comunicado:
                print(f"Comunicado: {resultado.comunicado}")

            if CONFIRMAR_IRREGULAR:
                print("Como existe local de votacao, ele sera atualizado no CRM apos sua confirmacao.")
                confirm = ask_user_timeout(
                    "Confira visualmente o resultado no TSE. Digite S para salvar no CRM, ou R para deixar no repasse: ",
                    ENTRADA_MANUAL_TIMEOUT_S,
                    default="R",
                ).strip().upper()
                if confirm != "S":
                    print("Sem confirmacao manual. A pessoa fica para o repasse.")
                    return False
            else:
                print("Existe local de votacao. Salvando no CRM automaticamente.")
        else:
            print("Pessoa regular sem alerta. Salvando automaticamente no CRM.")

        if not atualizar_crm(crm, pessoa, resultado.texto_para_crm):
            print("Nao confirmei o salvamento do local. A pessoa continua pendente.")
            return False

        motivo = motivo_inativacao(resultado)
        if motivo:
            # Inativacao falhando nao invalida o local ja gravado: registra e segue.
            try:
                inativar_cadastro_validado(crm, pessoa, motivo)
            except Exception as exc:
                registrar_erro(pessoa, 0, exc)
                print(f"Nao consegui inativar automaticamente: {resumo_erro(exc)}")
                print("Confira/inative manualmente no CRM antes de seguir.")

        print("Salvei no CRM. Indo para a proxima pessoa...")
        if resultado.irregular and CONFIRMAR_IRREGULAR:
            ask_user_timeout("Pressione Enter para ir para a proxima pessoa...", ENTRADA_MANUAL_TIMEOUT_S)
        return True
    finally:
        # O callback encerra a sessao e a instancia exclusivas desta pessoa.
        fechar_tse_resultado(resultado)
        try:
            voltar_para_pendentes(crm)
        except Exception:
            pass


def navegador_caiu(exc: Exception) -> bool:
    texto = normalize_text(str(exc))
    return any(
        marca in texto
        for marca in (
            "TARGET PAGE, CONTEXT OR BROWSER HAS BEEN CLOSED",
            "BROWSER HAS BEEN CLOSED",
            "BROWSER CLOSED",
            "CONNECTION CLOSED",
        )
    )


def resumo_erro(exc: Exception) -> str:
    primeira_linha = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {primeira_linha[:160]}"


def registrar_erro(pessoa: Pessoa, tentativa: int, exc: Exception) -> None:
    """Acrescenta o erro ao log. Nunca grava dado pessoal: so o CPF, que e a
    chave para reencontrar a pessoa no CRM."""
    carimbo = datetime.now().isoformat(timespec="seconds")
    cabecalho = f"\n{'=' * 70}\n{carimbo} | CPF {pessoa.cpf} | tentativa {tentativa}\n"
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(cabecalho)
            arquivo.write(traceback.format_exc())
    except OSError:
        pass


def recuperar_crm(page: Page) -> bool:
    """Devolve o CRM a um estado utilizavel depois de um erro."""
    page.wait_for_timeout(PAUSA_ENTRE_TENTATIVAS_MS)

    for fechar in (
        page.get_by_role("button", name=re.compile(r"Cancelar|Fechar", re.I)).last,
        page.locator("[role='dialog'] button").last,
    ):
        click_if_visible(page, fechar)

    if not ir_para(page, CRM_URL):
        return False

    try:
        page.locator("table tbody tr").first.wait_for(timeout=20000)
        return True
    except (TimeoutError, Error):
        return False


def renovar_crm_controlado(page: Page) -> bool:
    """Renova o CRM somente entre pessoas e confirma que a tela segura voltou."""
    print(f"Renovacao preventiva do CRM apos {CRM_RECARREGAR_A_CADA_PESSOAS} pessoa(s)...")
    try:
        if not ir_para(page, CRM_URL, tentativas=2):
            return False

        candidatos = (
            page.get_by_role("button", name=re.compile(r"^Pendentes", re.I)).first,
            page.get_by_text(re.compile(r"^Pendentes", re.I)).first,
        )
        pronto = False
        for candidato in candidatos:
            try:
                if candidato.is_visible(timeout=10000):
                    pronto = True
                    break
            except (TimeoutError, Error):
                continue
        if not pronto:
            return False

        voltar_para_pendentes(page)
        maximizar_por_pagina(page)
        print("CRM renovado e confirmado na tela de Pendentes.")
        return True
    except (TimeoutError, Error, NavegadorMorto):
        return False


def ir_para(page: Page, url: str, tentativas: int = 3) -> bool:
    """page.goto com repeticao.

    O CRM as vezes redireciona sozinho durante o carregamento e o Playwright
    aborta com "interrupted by another navigation" -- erro transitorio, que
    some ao tentar de novo.
    """
    for tentativa in range(1, tentativas + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return True
        except (TimeoutError, Error) as exc:
            if navegador_caiu(exc):
                raise NavegadorMorto() from exc
            print(f"  navegacao falhou ({tentativa}/{tentativas}): {resumo_erro(exc)}")
            page.wait_for_timeout(PAUSA_ENTRE_TENTATIVAS_MS)
    return False


def perguntar_operador() -> tuple[int, int, int, int, int]:
    """Pergunta operador, divisao, teto e configuracao das pausas.

    A fatia sai de sha1(cpf) % total, entao os operadores nao precisam se falar:
    a divisao e a mesma em toda maquina e estavel entre execucoes.
    """
    global CRM_DIVISAO_FIXA_QUATRO

    while True:
        modo_divisao = ask_user(
            "Como dividir Pendentes? [Enter/1 = configurar operadores, 2 = 4 blocos fixos] "
        ).strip()
        if modo_divisao in ("", "1"):
            CRM_DIVISAO_FIXA_QUATRO = False
            break
        if modo_divisao == "2":
            CRM_DIVISAO_FIXA_QUATRO = True
            break
        print("Opcao invalida. Digite 1 ou 2.")

    if CRM_DIVISAO_FIXA_QUATRO:
        total = 4
        numero = ler_inteiro("Qual bloco vai rodar agora (0 a 3)? ", minimo=0, maximo=3)
        print(f"Pendentes: bloco fixo {numero} de 4.")
        print("Para cobrir 100%, execute os blocos 0, 1, 2 e 3.")
    else:
        total = ler_inteiro("Quantos operadores vao rodar agora (1 = so voce)? ", minimo=1, maximo=50)

        if total == 1:
            numero = 0
            print("Rodando sozinho: a fila inteira e sua.")
        else:
            numero = ler_inteiro(f"Voce e o operador numero (0 a {total - 1})? ", minimo=0, maximo=total - 1)
            print(f"Operador {numero} de {total}.")
            print("ATENCAO: so as fatias que forem rodadas serao processadas. Para cobrir 100%,")
            print(f"os numeros 0 a {total - 1} precisam rodar.")

    limite_padrao = 0 if CRM_DIVISAO_FIXA_QUATRO else LIMITE_PADRAO
    mensagem_limite = (
        "Quantos CPFs no maximo nesta rodada? [Enter/0 = bloco inteiro] "
        if CRM_DIVISAO_FIXA_QUATRO
        else f"Quantos CPFs no maximo nesta rodada? [Enter = {LIMITE_PADRAO}, 0 = sem limite] "
    )
    limite = ler_inteiro(
        mensagem_limite,
        minimo=0,
        maximo=100000,
        padrao=limite_padrao,
    )
    print("Sem teto: vou ate o fim da sua fatia." if limite == 0 else f"Teto desta rodada: {limite} CPF(s).")
    pausar_a_cada = ler_inteiro(
        "Fazer uma pausa a cada quantas pessoas? [Enter = 10, minimo = 1] ",
        minimo=1,
        maximo=100000,
        padrao=10,
    )
    duracao_pausa_segundos = ler_inteiro(
        "Quantos segundos deve durar cada pausa? [Enter = 10, minimo = 1] ",
        minimo=1,
        maximo=3600,
        padrao=10,
    )
    print(f"Pausa configurada: {duracao_pausa_segundos}s a cada {pausar_a_cada} pessoa(s).")
    return numero, total, limite, pausar_a_cada, duracao_pausa_segundos


def ler_inteiro(mensagem: str, minimo: int, maximo: int, padrao: int | None = None) -> int:
    while True:
        resposta = ask_user(mensagem).strip()
        if not resposta and padrao is not None:
            return padrao
        if resposta.isdigit() and minimo <= int(resposta) <= maximo:
            return int(resposta)
        print(f"Valor invalido. Informe um numero inteiro entre {minimo} e {maximo}.")


def fatia_do_cpf(cpf: str, total: int) -> int:
    """Fatia estavel a partir do CPF.

    Usa sha1 em vez de int(cpf) % total: o ultimo digito do CPF e verificador e
    distribui mal. Medido em 166 CPFs reais, o modulo direto dava 33 pessoas para
    uma fatia e 10 para outra; o hash ficou entre 13 e 23.
    """
    if total <= 1:
        return 0
    return int(hashlib.sha1(cpf.encode("utf-8")).hexdigest(), 16) % total


def inventariar_com_retentativa(page: Page) -> list[Pessoa]:
    """O inventario e a base de tudo: se falhar, nao ha fila. Vale insistir."""
    for tentativa in range(1, TENTATIVAS_POR_PESSOA + 1):
        try:
            encontrados = inventariar_pendentes(page)
            if encontrados:
                return encontrados
            print(f"Inventario veio vazio (tentativa {tentativa}/{TENTATIVAS_POR_PESSOA}).")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if navegador_caiu(exc):
                raise
            print(f"Inventario falhou ({tentativa}/{TENTATIVAS_POR_PESSOA}): {resumo_erro(exc)}")

        if tentativa < TENTATIVAS_POR_PESSOA:
            recuperar_crm(page)
    return []


def inventariar_pendentes(page: Page) -> list[Pessoa]:
    """Varre TODAS as paginas de Pendentes e devolve as pessoas legiveis.

    Precisa ser feito de uma vez, antes de qualquer consulta ao TSE: o fluxo
    antigo so enxergava a pagina 1, e com fatiamento cada operador esgotaria a
    janela visivel e pararia achando que a fila acabou.
    """
    voltar_para_pendentes(page)
    maximizar_por_pagina(page)

    encontrados: dict[str, Pessoa] = {}
    for pagina in range(1, MAX_PAGINAS_INVENTARIO + 1):
        antes = len(encontrados)
        for row_index in range(total_rows(page)):
            pessoa = read_person_from_row(page, row_index)
            if pessoa:
                encontrados.setdefault(pessoa.cpf, pessoa)

        novos = len(encontrados) - antes
        print(f"  pagina {pagina}: +{novos} pessoa(s) (total {len(encontrados)})")

        if not ir_para_proxima_pagina(page):
            break
        if novos == 0:
            # Paginou mas nada novo apareceu: trata como fim para nao girar em falso.
            break

    return list(encontrados.values())


def maximizar_por_pagina(page: Page) -> None:
    """Prefere uma pagina menor para limitar DOM e memoria do CRM."""
    for select in page.locator("select").all():
        try:
            numericas = sorted(
                {int(o.strip()) for o in select.locator("option").all_text_contents() if o.strip().isdigit()}
            )
        except (TimeoutError, Error):
            continue
        if len(numericas) < 2:
            continue

        if CRM_ITENS_POR_PAGINA in numericas:
            escolhido = CRM_ITENS_POR_PAGINA
        else:
            menores = [valor for valor in numericas if valor <= CRM_ITENS_POR_PAGINA]
            escolhido = max(menores) if menores else min(numericas)

        try:
            select.select_option(label=str(escolhido), timeout=8000)
            page.wait_for_timeout(1500)
            print(f"Itens por pagina: {escolhido} (modo de memoria reduzida)")
            return
        except (TimeoutError, Error):
            continue


def ir_para_proxima_pagina(page: Page) -> bool:
    """Avanca uma pagina. Devolve False quando nao ha proxima."""
    candidatos = (
        page.get_by_role("button", name=re.compile(r"^(pr[óo]xim[ao]|next|seguinte)", re.I)).first,
        page.get_by_role("link", name=re.compile(r"^(pr[óo]xim[ao]|next|seguinte)", re.I)).first,
        page.locator("button[aria-label*='rox' i], a[aria-label*='rox' i]").first,
        page.locator("button, a").filter(has_text=re.compile(r"^\s*(›|»|>)\s*$")).first,
    )

    primeira_linha = texto_da_primeira_linha(page)
    for alvo in candidatos:
        try:
            if not alvo.is_visible(timeout=1000) or not alvo.is_enabled(timeout=1000):
                continue
            alvo.click(timeout=8000)
        except (TimeoutError, Error):
            continue

        page.wait_for_timeout(1500)
        # Confirma que a tabela realmente mudou: botao habilitado na ultima
        # pagina e comum e faria o laco girar sem sair do lugar.
        if texto_da_primeira_linha(page) != primeira_linha:
            return True
    return False


def texto_da_primeira_linha(page: Page) -> str:
    try:
        return clean(page.locator("table tbody tr").first.inner_text(timeout=5000))
    except (TimeoutError, Error):
        return ""


def abrir_pessoa_por_cpf(page: Page, pendente: Pessoa) -> Pessoa | None:
    """Filtra Pendentes pelo CPF e rele os dados da linha.

    Buscar em vez de usar a posicao guardada e o que torna a execucao paralela
    segura: a linha e achada mesmo que esteja na pagina 30, e nunca se escreve
    numa pessoa que apenas herdou o indice de outra.
    """
    voltar_para_pendentes(page)
    try:
        fill_search(page, pendente.cpf)
    except (TimeoutError, Error):
        print("Nao consegui usar o campo de busca do CRM.")
        return None

    for row_index in range(total_rows(page)):
        pessoa = read_person_from_row(page, row_index)
        if pessoa and pessoa.cpf == pendente.cpf:
            return pessoa
    return None


def read_person_from_row(page: Page, row_index: int) -> Pessoa | None:
    rows = page.locator("table tbody tr")
    if row_index >= rows.count():
        return None

    headers = header_indexes(page)
    row = rows.nth(row_index)
    cells = row.locator("td")

    def cell(*names: str) -> str:
        for name in names:
            idx = headers.get(normalize_header(name))
            if idx is not None and idx < cells.count():
                return clean(cells.nth(idx).inner_text(timeout=5000))
        return ""

    nome = clean_person_name(cell("NOME COMPLETO", "NOME"))
    cpf = only_digits(cell("CPF"))
    mae = cell("NOME DA MÃE", "NOME DA MAE", "MAE", "MÃE")
    nascimento = cell("NASCIMENTO", "DATA NASCIMENTO", "DATA DE NASCIMENTO")

    # Mae e nascimento sao obrigatorios no TSE, mas a ausencia deles nao pode
    # excluir a pessoa do inventario: o caso sera tratado diretamente no CRM.
    # O CPF continua obrigatorio porque e a chave segura para reencontrar a
    # linha, inclusive quando ha mais de um operador trabalhando.
    if not cpf:
        print(f"Dados incompletos na linha {row_index + 1}: nome={nome!r}, cpf={cpf!r}, mae={mae!r}, nasc={nascimento!r}")
        return None

    return Pessoa(row_index=row_index, nome=nome, cpf=cpf, mae=mae, nascimento=nascimento)


def voltar_para_pendentes(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"^Pendentes", re.I)).first.click(timeout=8000)
    except (TimeoutError, Error):
        try:
            page.get_by_text(re.compile(r"^Pendentes", re.I)).first.click(timeout=8000)
        except (TimeoutError, Error):
            pass

    page.wait_for_timeout(800)
    clear_search(page)

    try:
        page.locator("table tbody tr").first.wait_for(timeout=12000)
    except TimeoutError:
        pass


def clear_search(page: Page) -> None:
    try:
        search = page.get_by_placeholder(re.compile(r"Buscar|CPF|nome", re.I)).first
        if search.is_visible(timeout=1000):
            search.fill("", timeout=5000)
            page.wait_for_timeout(800)
    except (TimeoutError, Error):
        pass


def header_indexes(page: Page) -> dict[str, int]:
    headers: dict[str, int] = {}
    ths = page.locator("table thead th")
    for idx in range(ths.count()):
        label = normalize_header(ths.nth(idx).inner_text(timeout=5000))
        if label:
            headers[label] = idx
    return headers


def consultar_tse(playwright, context: BrowserContext, pessoa: Pessoa) -> ResultadoTse:
    if TSE_NO_CHROME_NORMAL:
        return consultar_tse_chrome_normal(playwright, pessoa)

    page = context.new_page()
    try:
        return consultar_tse_playwright_page(page, pessoa)
    finally:
        page.close()


def consultar_tse_playwright_page(page: Page, pessoa: Pessoa) -> ResultadoTse:
    # Teto absoluto: nenhuma pessoa pode consumir a execucao inteira. Sem isto,
    # 3 tentativas x espera longa de CAPTCHA travariam a fila por muito tempo.
    prazo_final = time.monotonic() + (TEMPO_MAXIMO_POR_PESSOA_MS / 1000)

    for tentativa in range(1, MAX_TSE_ATTEMPTS + 1):
        if time.monotonic() > prazo_final:
            print("Tempo maximo desta pessoa esgotado. Deixo para o repasse e sigo.")
            return resultado_sem_identificacao(
                "ERRO NAO IDENTIFICADO",
                "Tempo maximo por pessoa esgotado (CAPTCHA nao resolvido ou TSE lento).",
            )

        print(f"Tentativa TSE {tentativa}/{MAX_TSE_ATTEMPTS}")
        # A tentativa anterior pode deixar o iframe do desafio sobre a SPA.
        recarregar_se_captcha_anterior(page)
        if not ir_para(page, TSE_URL, tentativas=2):
            continue
        if eleitor_tse_autenticado(page) and not desautenticar_eleitor_tse(page):
            print("O perfil do eleitor anterior continuou ativo. Nao vou consultar, salvar nem avancar a fila.")
            raise PerfilTsePreso("O TSE nao encerrou o perfil do eleitor anterior.")
        if not abrir_onde_votar(page):
            if tentativa < MAX_TSE_ATTEMPTS:
                print("A autenticacao nao ficou disponivel. Vou tentar esta pessoa novamente.")
                continue
            return resultado_sem_identificacao(
                "ERRO NAO IDENTIFICADO",
                "Tela de autenticacao/CAPTCHA indisponivel apos todas as tentativas.",
            )
        if not preencher_autenticacao(page, pessoa):
            if tentativa < MAX_TSE_ATTEMPTS:
                print("Tela do TSE ainda carregando/travada. Vou tentar esta pessoa novamente.")
                continue
            return resultado_sem_identificacao("ERRO NAO IDENTIFICADO", "Tela do TSE ficou carregando e nao permitiu clicar em Entrar.")

        if esperar_resultado_tse(page, prazo_final):
            break

        if tentativa < MAX_TSE_ATTEMPTS:
            print("Vou tentar a mesma pessoa novamente.")
            continue

        if PERGUNTAR_NO_TERMINAL:
            return consultar_tse_manual(
                pessoa,
                "Nao veio resposta do TSE apos varias tentativas ou o CAPTCHA ficou indisponivel.",
            )

        print("TSE nao respondeu apos todas as tentativas. Deixo esta pessoa para depois e sigo.")
        return resultado_sem_identificacao(
            "ERRO NAO IDENTIFICADO",
            "TSE nao respondeu ou CAPTCHA indisponivel apos todas as tentativas.",
        )

    else:
        # Todas as tentativas podem terminar em ``continue`` (por exemplo, se
        # o perfil anterior continuar autenticado). Nessa situacao nao existe
        # uma resposta valida do TSE para gravar no CRM.
        return resultado_sem_identificacao(
            "ERRO NAO IDENTIFICADO",
            "Nao foi possivel completar a consulta apos as tentativas.",
        )

    texto = clean(page.locator("body").inner_text(timeout=30000))
    if has_auth_retry_error(texto) or has_captcha_error(texto):
        return resultado_sem_identificacao(
            "ERRO NAO IDENTIFICADO",
            "A consulta terminou em uma tela temporaria de autenticacao/CAPTCHA.",
        )
    status = extract_status(texto)
    comunicado = extract_comunicado(texto)
    encontrado = has_voting_place_result(texto)

    if encontrado:
        texto_para_crm = montar_texto_crm(texto, status, comunicado)
    else:
        status = status or "ERRO NAO IDENTIFICADO"
        comunicado = comunicado or extract_negative_message(texto) or "Pessoa nao localizada no TSE ou dados nao conferem. Conferir CPF, nome da mae e nascimento."
        texto_para_crm = f"{status}: {comunicado}"

    irregular = is_irregular(status, comunicado, texto)

    resumo_terminal = clean(" | ".join(parte for parte in (status, comunicado) if parte))
    if not resumo_terminal:
        resumo_terminal = "resultado recebido"
    print(f"Resultado TSE: {resumo_terminal[:220]}")
    return ResultadoTse(
        texto_para_crm=texto_para_crm,
        status=status,
        comunicado=comunicado,
        irregular=irregular,
        encontrado=encontrado,
    )


def consultar_tse_chrome_normal(playwright, pessoa: Pessoa) -> ResultadoTse:
    global _TSE_BROWSER, _TSE_PROCESS, _TSE_CONTEXT
    # Cada pessoa recebe processo e aba proprios. Qualquer sobra de uma
    # execucao interrompida e encerrada antes de ocupar a porta novamente.
    encerrar_sessao_tse()
    _TSE_PROCESS = abrir_tse_no_chrome_normal()
    try:
        _TSE_BROWSER = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{TSE_REMOTE_DEBUGGING_PORT}"
        )
        _TSE_CONTEXT = (
            _TSE_BROWSER.contexts[0]
            if _TSE_BROWSER.contexts
            else _TSE_BROWSER.new_context()
        )
        page = preparar_aba_exclusiva_tse(_TSE_CONTEXT)
        recarregar_se_captcha_anterior(page)
        resultado = consultar_tse_playwright_page(page, pessoa)
    except BaseException:
        encerrar_sessao_tse()
        raise

    resultado.fechar_tse = lambda: encerrar_consulta_isolada_tse(page)
    return resultado


def pagina_tse(context) -> Page:
    paginas = [page for page in context.pages if not page.is_closed()]
    for page in reversed(paginas):
        try:
            if "tse.jus.br" in page.url.lower():
                return page
        except Error:
            continue
    return context.new_page()


def preparar_aba_exclusiva_tse(context) -> Page:
    """Cria a aba desta pessoa e elimina abas restauradas pelo navegador."""
    abas_anteriores = [page for page in context.pages if not page.is_closed()]
    page = context.new_page()
    try:
        page.goto(TSE_URL, wait_until="domcontentloaded", timeout=45000)
    except (TimeoutError, Error):
        pass

    for aba in abas_anteriores:
        try:
            aba.close(run_before_unload=False)
        except Error:
            pass

    print("Nova aba exclusiva do TSE criada; abas restauradas da execucao anterior foram fechadas.")
    return page


def encerrar_consulta_isolada_tse(page: Page) -> None:
    """Encerra o eleitor atual e fecha a instancia usada por esta pessoa."""
    print("Finalizando a sessao do TSE desta pessoa...")
    try:
        if not page.is_closed():
            fechar_modal_informacao_tse(page)
            saiu = desautenticar_eleitor_tse(page)
            if not saiu:
                try:
                    page.goto(TSE_URL, wait_until="domcontentloaded", timeout=12000)
                    wait_tse_loading(page, timeout_ms=5000)
                    saiu = desautenticar_eleitor_tse(page)
                except (TimeoutError, Error):
                    pass
            if not saiu:
                print("A saida do eleitor nao foi confirmada; fechando esta instancia mesmo assim.")
    except (TimeoutError, Error, NavegadorMorto) as exc:
        print(f"Aviso ao encerrar a sessao do eleitor: {resumo_erro(exc)}")
    finally:
        encerrar_sessao_tse()


def fechar_modal_informacao_tse(page: Page) -> bool:
    """Fecha rapidamente o modal de resultado que bloqueia os controles de saida."""
    mensagem = page.get_by_text(
        re.compile(r"N[aã]o foi poss[ií]vel localizar um eleitor|Situa[cç][aã]o.*t[ií]tulo eleitoral", re.I)
    ).first
    try:
        if not mensagem.is_visible(timeout=800):
            return False
    except (TimeoutError, Error):
        return False

    nome_fechar = re.compile(r"^\s*Fechar\s*$", re.I)
    candidatos = (
        page.get_by_role("dialog").get_by_role("button", name=nome_fechar).last,
        page.get_by_role("button", name=nome_fechar).last,
    )
    for botao in candidatos:
        if click_if_visible(page, botao):
            print("Modal de informacoes do TSE fechado.")
            return True
    return False


def recarregar_se_captcha_anterior(page: Page) -> None:
    """Remove somente um desafio visual grande deixado pela pessoa anterior."""
    if not desafio_captcha_aberto(page):
        return
    print("CAPTCHA da consulta anterior ainda estava aberto. Recarregando a pagina inteira antes do proximo CPF.")
    try:
        page.reload(wait_until="domcontentloaded", timeout=45000)
        wait_tse_loading(page, timeout_ms=20000)
    except (TimeoutError, Error):
        ir_para(page, TSE_URL, tentativas=2)


def desafio_captcha_aberto(page: Page) -> bool:
    iframes = page.locator(
        "iframe[title*='challenge' i], iframe[src*='hcaptcha.com'], iframe[src*='newassets.hcaptcha.com']"
    )
    try:
        total = min(iframes.count(), 10)
    except (TimeoutError, Error):
        return False
    for indice in range(total):
        iframe = iframes.nth(indice)
        try:
            if not iframe.is_visible(timeout=300):
                continue
            titulo = (iframe.get_attribute("title", timeout=1000) or "").lower()
            caixa = iframe.bounding_box(timeout=1000)
            if "challenge" in titulo or (
                caixa is not None and caixa["width"] >= 250 and caixa["height"] >= 200
            ):
                return True
        except (TimeoutError, Error):
            continue
    return False


def encerrar_sessao_tse() -> None:
    global _TSE_BROWSER, _TSE_PROCESS, _TSE_CONTEXT
    if _TSE_BROWSER is None and _TSE_PROCESS is None:
        return
    encerrar_tse(_TSE_BROWSER, _TSE_PROCESS)
    _TSE_BROWSER = None
    _TSE_PROCESS = None
    _TSE_CONTEXT = None


def eleitor_tse_autenticado(page: Page) -> bool:
    indicador = page.get_by_text(re.compile(r"Eleitor\s*/?\s*Eleitora\s+com\s+CPF", re.I)).first
    try:
        return indicador.is_visible(timeout=1500)
    except (TimeoutError, Error):
        return False


def desautenticar_eleitor_tse(page: Page) -> bool:
    if not eleitor_tse_autenticado(page):
        return True

    # Nessa tela o TSE ainda conserva o perfil, mas esconde os controles de
    # saida. "Tentar novamente" devolve o fluxo para um estado em que o link
    # "Nao sou este eleitor" pode reaparecer.
    try:
        texto = page.locator("body").inner_text(timeout=5000)
        if has_auth_retry_error(texto):
            clicar_tentar_novamente(page)
            page.wait_for_timeout(1000)
    except (TimeoutError, Error):
        pass

    nao_sou = re.compile(r"sou\s+este\s+eleitor", re.I)
    sair = re.compile(r"sair|encerrar|trocar\s+eleitor", re.I)
    candidatos = (
        page.get_by_role("link", name=nao_sou).first,
        page.get_by_text(nao_sou).first,
        page.locator("a, button").filter(has_text=nao_sou).first,
        page.locator("[aria-label*='sair' i], [title*='sair' i], [class*='logout' i], [class*='sign-out' i]").first,
        page.locator("button:has([class*='sign-out']), a:has([class*='sign-out']), button:has([class*='logout']), a:has([class*='logout'])").first,
        page.get_by_role("button", name=sair).last,
    )
    for controle in candidatos:
        try:
            if not controle.is_visible(timeout=1500):
                continue
            controle.click(timeout=8000)
            page.wait_for_timeout(1000)
            fechar_aviso_saida_tse(page)
            if not eleitor_tse_autenticado(page):
                print("Sessao do eleitor anterior encerrada.")
                return True
        except (TimeoutError, Error):
            continue
    return False


def encerrar_tse(browser, chrome_process) -> None:
    """Fecha o Chrome do TSE de verdade e libera a porta de controle.

    browser.close() sobre uma conexao CDP apenas DESCONECTA -- o processo do
    Chrome continua vivo. Era por isso que a porta seguia respondendo e a
    consulta seguinte caia numa janela com a tela da consulta anterior.
    """
    if browser is not None:
        try:
            # Solicita fechamento normal para o perfil nao ser marcado como
            # interrompido e restaurar abas antigas na proxima pessoa.
            browser.new_browser_cdp_session().send("Browser.close")
        except (Error, OSError):
            pass
        finally:
            try:
                browser.close()
            except (Error, OSError):
                pass

    if chrome_process is not None:
        try:
            chrome_process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for encerrar in (chrome_process.terminate, chrome_process.kill):
            if chrome_process.poll() is not None:
                break
            try:
                encerrar()
                chrome_process.wait(timeout=5)
                break
            except (OSError, subprocess.TimeoutExpired):
                continue

    if cdp_respondendo():
        matar_chrome_do_tse()
    esperar_porta_livre()


def matar_chrome_do_tse() -> None:
    """Mata os processos do Chrome abertos com o perfil do TSE.

    O Chrome se ramifica em varios processos e o que o Popen segura nem sempre
    e o que fica de pe. Filtrar pela linha de comando pega a arvore toda, e nao
    encosta no Chrome pessoal do operador: o perfil e exclusivo deste bot.
    """
    alvo = str(TSE_PROFILE_DIR)
    nome_processo = "brave.exe" if TSE_NAVEGADOR == "brave" else "chrome.exe"
    try:
        if sys.platform.startswith("win"):
            script = (
                f"Get-CimInstance Win32_Process -Filter \"Name='{nome_processo}'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{alvo}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            comando = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        else:
            comando = ["pkill", "-f", alvo]

        subprocess.run(
            comando,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            **popen_background_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def esperar_porta_livre(segundos: int = 10) -> bool:
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if not cdp_respondendo():
            return True
        time.sleep(0.5)
    return False


def cdp_respondendo() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{TSE_REMOTE_DEBUGGING_PORT}/json/version", timeout=1
        ):
            return True
    except OSError:
        return False


def abrir_onde_votar(page: Page) -> bool:
    wait_tse_loading(page)
    try:
        page.get_by_text(re.compile(r"onde votar", re.I)).first.click(timeout=8000)
    except (TimeoutError, Error):
        pass

    campo = page.locator("#titulo-cpf-nome").or_(
        page.get_by_role("textbox", name=re.compile(r"título eleitoral|titulo eleitoral|CPF", re.I))
    ).first
    wait_tse_loading(page)
    if pagina_com_erro_captcha(page):
        try:
            campo.wait_for(timeout=15000)
            return True
        except TimeoutError:
            return False
    try:
        campo.wait_for(timeout=15000)
        return True
    except TimeoutError:
        pass

    if pagina_com_erro_captcha(page):
        try:
            campo.wait_for(timeout=15000)
            return True
        except TimeoutError:
            return False

    if PERGUNTAR_NO_TERMINAL:
        ask_user_timeout(
            "Nao achei a tela de autenticacao do TSE. Abra 'Onde Votar' manualmente e pressione Enter...",
            ENTRADA_MANUAL_TIMEOUT_S,
        )
        return campo.is_visible(timeout=1500)

    # Sem parar o fluxo: recarrega e tenta abrir "Onde Votar" de novo. Se ainda
    # assim nao aparecer, quem chamou trata como tentativa perdida.
    print("Nao achei a tela de autenticacao do TSE. Recarregando...")
    if not ir_para(page, TSE_URL, tentativas=2):
        return False
    wait_tse_loading(page)
    try:
        page.get_by_text(re.compile(r"onde votar", re.I)).first.click(timeout=8000)
    except (TimeoutError, Error):
        pass
    wait_tse_loading(page)
    if pagina_com_erro_captcha(page):
        try:
            campo.wait_for(timeout=15000)
            return True
        except TimeoutError:
            return False
    try:
        campo.wait_for(timeout=15000)
        return True
    except TimeoutError:
        print("A tela de autenticacao do TSE nao abriu. Sigo para a proxima tentativa.")
        return False


def pagina_com_erro_captcha(page: Page) -> bool:
    try:
        texto = page.locator("body").inner_text(timeout=5000)
    except (TimeoutError, Error):
        return False
    if not has_captcha_error(texto):
        return False
    limpar_erro_captcha(page)
    return True


def preencher_autenticacao(page: Page, pessoa: Pessoa) -> bool:
    # Campo que nao aparece e falha desta tentativa, nao da execucao: devolve
    # False e o laco de tentativas do TSE recomeca com a pagina recarregada.
    try:
        return _preencher_autenticacao(page, pessoa)
    except RuntimeError as exc:
        pagina_com_erro_captcha(page)
        print(f"  autenticacao do TSE falhou: {resumo_erro(exc)}")
        return False


def _preencher_autenticacao(page: Page, pessoa: Pessoa) -> bool:
    wait_tse_loading(page)
    fill_first_locator(
        page,
        (
            "#titulo-cpf-nome",
            "input[formcontrolname='TituloCPFNome']",
            "input[aria-label*='CPF' i]",
        ),
        pessoa.cpf,
    )
    fill_first_locator(
        page,
        (
            "input[formcontrolname='dataNascimento']",
            "input[aria-label*='nascimento' i]",
            "input[placeholder*='nascimento' i]",
        ),
        pessoa.nascimento,
    )
    fill_first_locator(
        page,
        (
            "#nomeMae",
            "input[formcontrolname='nomeMae']",
            "input[aria-label='Nome da mãe']",
        ),
        pessoa.mae,
    )
    wait_tse_loading(page)
    try:
        page.get_by_role("button", name=re.compile(r"^\s*Entrar\s*$", re.I)).click(timeout=20000)
        return True
    except TimeoutError:
        wait_tse_loading(page, timeout_ms=30000)
        try:
            page.get_by_role("button", name=re.compile(r"^\s*Entrar\s*$", re.I)).click(timeout=10000)
            return True
        except TimeoutError:
            return False


def esperar_resultado_tse(page: Page, prazo_final: float | None = None) -> bool:
    """Espera a resposta do TSE sem pedir nada no terminal.

    O CAPTCHA continua sendo resolvido por voce no navegador -- o que sai daqui
    e a parada para digitar Enter. Enquanto houver CAPTCHA na tela, a espera e
    prorrogada (voce pode estar resolvendo); sem CAPTCHA, vale o prazo curto.
    Estourando o teto total, devolve False e quem chamou tenta de novo.
    """
    print("Aguardando resposta do TSE...")
    if PERGUNTAR_NO_TERMINAL:
        return _esperar_resultado_interativo(page)

    inicio = time.monotonic()
    teto = inicio + (TSE_ESPERA_MAXIMA_MS / 1000)
    if prazo_final is not None:
        teto = min(teto, prazo_final)
    deadline = inicio + (TSE_RESPONSE_TIMEOUT_MS / 1000)
    avisou = False
    leituras_falhas = 0
    proximo_aviso = inicio + 15

    while time.monotonic() < min(deadline, teto):
        page.wait_for_timeout(1000)
        agora = time.monotonic()
        if agora >= proximo_aviso:
            restante = max(0, int(min(deadline, teto) - agora))
            print(f"  TSE ainda sem resultado; aguardando ({restante}s restantes nesta espera).")
            proximo_aviso = agora + 15
        try:
            texto = page.locator("body").inner_text(timeout=10000)
            leituras_falhas = 0
        except (TimeoutError, Error) as exc:
            if navegador_caiu(exc):
                raise NavegadorMorto() from exc
            # Pagina ilegivel por muito tempo seguido: recomeca a tentativa em
            # vez de girar aqui ate o teto.
            leituras_falhas += 1
            if leituras_falhas >= 15:
                print("  Nao consigo ler a pagina do TSE. Vou reiniciar a consulta.")
                return False
            continue

        if has_auth_retry_error(texto):
            if retomar_consulta_apos_erro_autenticacao(page):
                deadline = min(time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000), teto)
                continue
            return False

        if has_captcha_error(texto):
            if limpar_erro_captcha(page):
                deadline = min(time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000), teto)
                continue
            return False

        if clicar_entrar_confirmacao(page):
            deadline = min(time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000), teto)
            continue

        if has_voting_place_result(texto) or has_tse_negative_response(texto):
            return True

        if has_captcha(page):
            if not avisou:
                print(">>> CAPTCHA na tela. Resolva no navegador: eu sigo sozinho depois. <<<")
                avisou = True
            deadline = min(time.monotonic() + (TSE_ESPERA_CAPTCHA_MS / 1000), teto)

    # Ultima leitura antes de desistir: a resposta pode ter chegado no ultimo segundo.
    try:
        texto = page.locator("body").inner_text(timeout=10000)
    except (TimeoutError, Error):
        return False

    if has_auth_retry_error(texto):
        if retomar_consulta_apos_erro_autenticacao(page) and time.monotonic() < teto:
            print("O botao apareceu no fim da espera; continuo nesta mesma consulta.")
            return esperar_resultado_tse(page, prazo_final)
        return False
    if has_captcha_error(texto):
        if limpar_erro_captcha(page) and time.monotonic() < teto:
            print("O servico voltou no fim da espera; continuo nesta mesma consulta.")
            return esperar_resultado_tse(page, prazo_final)
        return False
    return has_voting_place_result(texto) or has_tse_negative_response(texto)


def _esperar_resultado_interativo(page: Page) -> bool:
    """Comportamento antigo, com paradas no terminal. So com PERGUNTAR_NO_TERMINAL."""
    deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        texto = page.locator("body").inner_text(timeout=10000)
        if has_auth_retry_error(texto):
            if retomar_consulta_apos_erro_autenticacao(page):
                deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)
                continue
            return False
        if has_captcha_error(texto):
            if limpar_erro_captcha(page):
                deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)
                continue
            return False

        if clicar_entrar_confirmacao(page):
            deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)
            continue

        if has_voting_place_result(texto) or has_tse_negative_response(texto):
            return True

        if has_captcha(page):
            resposta = ask_user_timeout(
                "O TSE pediu validacao de robo. Resolva no navegador e pressione Enter aqui; digite R para reiniciar: ",
                ENTRADA_MANUAL_TIMEOUT_S,
                default="R",
            ).strip().upper()
            if resposta == "R":
                return False
            deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)

    resposta = ask_user_timeout(
        "Ainda nao identifiquei o resultado/modal do TSE. Pressione Enter para ler agora, ou R para repetir: ",
        ENTRADA_MANUAL_TIMEOUT_S,
        default="R",
    ).strip().upper()
    if resposta == "R":
        return False

    texto = page.locator("body").inner_text(timeout=10000)
    if has_auth_retry_error(texto):
        if retomar_consulta_apos_erro_autenticacao(page):
            return _esperar_resultado_interativo(page)
        return False
    if has_captcha_error(texto):
        limpar_erro_captcha(page)
        return False
    return has_voting_place_result(texto) or has_tse_negative_response(texto)


def has_captcha(page: Page) -> bool:
    for selector in CAPTCHA_SELECTORS:
        try:
            if page.locator(selector).first.is_visible(timeout=700):
                return True
        except (TimeoutError, Error):
            continue
    return False


def fechar_aviso_saida_tse(page: Page) -> bool:
    aviso = page.get_by_text(re.compile(r"saindo\s+da\s+[aá]rea\s+de\s+autoatendimento", re.I)).first
    try:
        if not aviso.is_visible(timeout=1500):
            return False
    except (TimeoutError, Error):
        return False

    dialogo = page.get_by_role("dialog").filter(has_text=re.compile(r"saindo\s+da\s+[aá]rea", re.I)).first
    candidatos = (
        dialogo.get_by_role("button", name=re.compile(r"^\s*Fechar\s*$", re.I)).first,
        page.get_by_role("button", name=re.compile(r"^\s*Fechar\s*$", re.I)).last,
    )
    for botao in candidatos:
        if click_if_visible(page, botao):
            print("Aviso de saida do TSE fechado.")
            wait_tse_loading(page, timeout_ms=20000)
            return True
    return False


def clicar_entrar_confirmacao(page: Page) -> bool:
    """Confirma a etapa intermediaria sem selecionar o acesso por e-Titulo."""
    try:
        texto = normalize_text(page.locator("body").inner_text(timeout=5000))
    except (TimeoutError, Error):
        return False

    mensagem = "PARA ACESSAR ESTE SERVICO E NECESSARIO INFORMAR TAMBEM"
    if mensagem not in texto:
        return False

    nome_entrar = re.compile(r"^\s*Entrar\s*$", re.I)
    grupos = (
        page.get_by_role("button", name=nome_entrar),
        page.locator("button, [role='button']").filter(has_text=nome_entrar),
    )
    for grupo in grupos:
        try:
            total = min(grupo.count(), 10)
        except (TimeoutError, Error):
            continue
        for indice in range(total):
            if click_if_visible(page, grupo.nth(indice)):
                print("Confirmacao intermediaria detectada. Cliquei em 'Entrar' e continuo aguardando o TSE.")
                wait_tse_loading(page, timeout_ms=20000)
                return True

    print("Confirmacao intermediaria detectada, mas o botao 'Entrar' ainda nao ficou clicavel.")
    return False


def retomar_consulta_apos_erro_autenticacao(page: Page) -> bool:
    """Executa Tentar novamente -> Entrar sem reiniciar o CPF atual."""
    if not clicar_tentar_novamente(page):
        return False

    print("Aguardando a confirmacao intermediaria desta mesma consulta...")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if clicar_entrar_confirmacao(page):
            print("Fluxo retomado: Tentar novamente -> Entrar -> aguardando o modal de informacoes.")
            return True

        try:
            texto = page.locator("body").inner_text(timeout=5000)
            if has_voting_place_result(texto) or has_tse_negative_response(texto):
                print("O TSE abriu diretamente o modal de informacoes apos 'Tentar novamente'.")
                return True
        except (TimeoutError, Error) as exc:
            if navegador_caiu(exc):
                raise NavegadorMorto() from exc

        page.wait_for_timeout(500)

    # Algumas respostas do TSE demoram mais; o laco principal continua
    # observando a mesma pagina sem refazer CPF, nascimento e nome da mae.
    print("A tela seguinte ainda nao apareceu; continuo aguardando esta mesma consulta.")
    return True


def has_captcha_error(texto: str) -> bool:
    normalized = normalize_text(texto)
    return any(term in normalized for term in CAPTCHA_ERROR_TERMS)


def has_auth_retry_error(texto: str) -> bool:
    normalized = normalize_text(texto)
    return any(term in normalized for term in AUTH_RETRY_ERROR_TERMS)


def clicar_tentar_novamente(page: Page) -> bool:
    botao = page.get_by_role("button", name=re.compile(r"Tentar\s+novamente", re.I)).first
    if not click_if_visible(page, botao):
        botao = page.get_by_text(re.compile(r"^\s*Tentar\s+novamente\s*$", re.I)).first
        if not click_if_visible(page, botao):
            print("Tela de autenticacao temporaria detectada, mas o botao 'Tentar novamente' nao estava disponivel.")
            return False
    print("Tela de autenticacao temporaria detectada. Cliquei em 'Tentar novamente'.")
    wait_tse_loading(page, timeout_ms=20000)
    return True


def limpar_erro_captcha(page: Page) -> bool:
    print("Erro/indisponibilidade de CAPTCHA. Tentando continuar pelo botao 'Tentar novamente'.")
    botao = page.get_by_role("button", name=re.compile(r"Tentar\s+novamente", re.I)).first
    if not click_if_visible(page, botao):
        botao = page.get_by_text(re.compile(r"^\s*Tentar\s+novamente\s*$", re.I)).first
        if not click_if_visible(page, botao):
            print("O botao 'Tentar novamente' nao estava disponivel; esta tentativa sera reiniciada.")
            return False
    wait_tse_loading(page, timeout_ms=20000)
    print("Cliquei em 'Tentar novamente' e continuo na mesma consulta.")
    return True


def click_if_visible(page: Page, locator) -> bool:
    try:
        if locator.is_visible(timeout=1500):
            locator.click(timeout=5000)
            page.wait_for_timeout(500)
            return True
    except (TimeoutError, Error):
        return False
    return False


def wait_tse_loading(page: Page, timeout_ms: int = 20000) -> None:
    loading_selectors = (
        "app-loading-spinner",
        ".loading-spinner",
        ".spinner",
        ".ngx-spinner-overlay",
        "text=/carregando|aguarde/i",
    )
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            visible = False
            for selector in loading_selectors:
                locator = page.locator(selector).first
                try:
                    if locator.is_visible(timeout=250):
                        visible = True
                        break
                except (TimeoutError, Error):
                    continue
            if not visible:
                return
        except Error:
            return
        page.wait_for_timeout(500)


def resultado_sem_identificacao(status: str, comunicado: str) -> ResultadoTse:
    # So chega aqui quando a consulta nao completou (CAPTCHA, timeout, tela travada),
    # nunca quando o TSE respondeu que nao localizou o eleitor.
    return ResultadoTse(
        texto_para_crm=f"{status}: {comunicado}",
        status=status,
        comunicado=comunicado,
        irregular=False,
        encontrado=False,
        resposta_do_tse=False,
    )


def fechar_tse_resultado(resultado: ResultadoTse) -> None:
    if not resultado.fechar_tse:
        return
    try:
        resultado.fechar_tse()
    finally:
        resultado.fechar_tse = None


def consultar_tse_manual(pessoa: Pessoa, motivo: str) -> ResultadoTse:
    print("\nO TSE nao deixou a consulta automatizada continuar.")
    print(motivo)
    print("Consulte manualmente no Chrome normal que foi aberto e cole aqui o resultado.")
    print(f"Nome: {pessoa.nome}")
    print(f"CPF: {pessoa.cpf}")
    print(f"Mae: {pessoa.mae}")
    print(f"Nascimento: {pessoa.nascimento}")
    print("Cole o texto do TSE abaixo. Quando terminar, digite FIM em uma linha sozinha.")
    print("Se quiser pular e registrar erro, digite PULAR.")

    pasted = ask_multiline_until_end(timeout_s=ENTRADA_MANUAL_TIMEOUT_S)
    if normalize_text(pasted) == "PULAR":
        return resultado_sem_identificacao("ERRO NAO IDENTIFICADO", "Consulta manual pulada apos bloqueio/indisponibilidade do CAPTCHA.")

    texto = clean(pasted)
    status = extract_status(texto)
    comunicado = extract_comunicado(texto) or extract_negative_message(texto)
    encontrado = has_voting_place_result(texto)

    if encontrado:
        texto_para_crm = montar_texto_crm(texto, status, comunicado)
    else:
        status = status or "ERRO NAO IDENTIFICADO"
        comunicado = comunicado or "Resultado manual nao continha local de votacao completo."
        texto_para_crm = f"{status}: {comunicado}"

    return ResultadoTse(
        texto_para_crm=texto_para_crm,
        status=status,
        comunicado=comunicado,
        irregular=is_irregular(status, comunicado, texto),
        encontrado=encontrado,
    )


def abrir_tse_no_chrome_normal():
    # Abre uma instancia para a pessoa atual. Se uma execucao anterior caiu e
    # deixou a porta presa, fecha somente o perfil exclusivo do TSE.
    if cdp_respondendo():
        print("Sobrou uma janela do TSE da consulta anterior. Fechando antes de abrir a nova...")
        matar_chrome_do_tse()
        if not esperar_porta_livre():
            print("A janela antiga do TSE nao fechou. Feche-a manualmente se travar.")

    navegador = find_tse_executable()
    try:
        process = subprocess.Popen(
            [
                navegador,
                f"--remote-debugging-port={TSE_REMOTE_DEBUGGING_PORT}",
                f"--user-data-dir={TSE_PROFILE_DIR}",
                "--no-first-run",
                "--new-window",
                TSE_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_background_kwargs(),
        )
        wait_cdp_ready()
        return process
    except OSError:
        print(f"Nao consegui abrir o navegador do TSE em {navegador}. Abra manualmente: {TSE_URL}")
        return None


def wait_cdp_ready(timeout_seconds: int = 12) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{TSE_REMOTE_DEBUGGING_PORT}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.4)
    raise RuntimeError("O navegador do TSE abriu, mas a porta de controle nao respondeu.")


def find_tse_executable() -> str:
    if TSE_NAVEGADOR == "chrome":
        return find_chrome_executable()
    if TSE_NAVEGADOR == "brave":
        return find_brave_executable()
    raise RuntimeError("TSE_NAVEGADOR deve ser 'chrome' ou 'brave'.")


def find_brave_executable() -> str:
    candidates = []
    if sys.platform.startswith("win"):
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.extend(
            [
                Path(program_files) / "BraveSoftware/Brave-Browser/Application/brave.exe",
                Path(program_files_x86) / "BraveSoftware/Brave-Browser/Application/brave.exe",
                Path(local_app_data) / "BraveSoftware/Brave-Browser/Application/brave.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"))
    else:
        for binary in ("brave-browser", "brave-browser-stable", "brave"):
            found = shutil.which(binary)
            if found:
                return found

    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)

    raise RuntimeError("Nao encontrei o Brave Browser instalado.")


def find_chrome_executable() -> str:
    if CHROME_EXECUTABLE:
        return CHROME_EXECUTABLE

    candidates = []
    if sys.platform.startswith("win"):
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.extend(
            [
                Path(program_files) / "Google/Chrome/Application/chrome.exe",
                Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
                Path(local_app_data) / "Google/Chrome/Application/chrome.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(binary)
            if found:
                return found
        candidates.extend([Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/snap/bin/chromium")])

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)

    raise RuntimeError("Nao encontrei o Google Chrome. Instale o Chrome ou configure CHROME_EXECUTABLE no inicio do script.")


def popen_background_kwargs() -> dict:
    if sys.platform.startswith("win"):
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def montar_texto_crm(texto: str, status: str, comunicado: str) -> str:
    lines = []
    add_field(lines, "Local de votação", extract_label_value(texto, "Local de votação"))
    add_field(lines, "Endereço", extract_label_value(texto, "Endereço"))
    add_field(lines, "Município/UF", extract_label_value(texto, "Município/UF"))
    add_field(lines, "Bairro", extract_label_value(texto, "Bairro"))
    add_field(lines, "Seção", extract_label_value(texto, "Seção"))
    add_field(lines, "País", extract_label_value(texto, "País"))
    add_field(lines, "Zona", extract_label_value(texto, "Zona"))

    if status:
        lines.append(f"Situação: {status}")
    if comunicado:
        lines.append(f"Comunicado: {comunicado}")

    if not lines:
        lines.append(texto[:3000])

    return "\n".join(lines)


def has_voting_place_result(texto: str) -> bool:
    normalized = normalize_text(texto)
    if "ESTE E O SEU LOCAL DE VOTACAO" in normalized or "ESTE É O SEU LOCAL DE VOTAÇÃO" in normalized:
        return True

    required_fields = (
        extract_label_value(texto, "Local de votação"),
        extract_label_value(texto, "Endereço"),
        extract_label_value(texto, "Município/UF"),
        extract_label_value(texto, "Seção"),
        extract_label_value(texto, "Zona"),
    )
    return sum(1 for value in required_fields if value) >= 3


def has_tse_negative_response(texto: str) -> bool:
    normalized = normalize_text(texto)
    negative_terms = (
        "DADOS NAO CONFEREM",
        "DADOS INFORMADOS NAO CONFEREM",
        "NAO FOI POSSIVEL LOCALIZAR",
        "NAO ENCONTRAMOS",
        "NAO ENCONTRADO",
        "NAO ENCONTRADA",
        "VERIFIQUE OS DADOS",
        "INFORMACOES INVALIDAS",
        "PESSOA NAO LOCALIZADA",
        "ELEITOR NAO LOCALIZADO",
    )
    return any(term in normalized for term in negative_terms)


def marcar_nao_achei(page: Page, pessoa: Pessoa) -> bool:
    """Clica no botao 'Nao achei' da linha da pessoa. Nao abre modal: e um clique so."""
    row = linha_da_pessoa(page, pessoa)
    if row is None:
        print(f"Nao localizei a linha de {pessoa.nome} para marcar 'Nao achei'. Confira manualmente no CRM.")
        return False

    try:
        row.scroll_into_view_if_needed(timeout=8000)
    except (TimeoutError, Error):
        pass

    botao = row.get_by_role("button", name=re.compile(r"N[ãa]o achei", re.I)).first
    try:
        botao.click(timeout=10000)
    except (TimeoutError, Error):
        # Fallback: em algumas telas o controle nao expoe role=button.
        try:
            row.get_by_text(re.compile(r"N[ãa]o achei", re.I)).first.click(timeout=8000)
        except (TimeoutError, Error):
            print("Nao encontrei o botao 'Nao achei' nesta linha. Confira manualmente no CRM.")
            return False

    page.wait_for_timeout(1500)
    print("Marquei 'Nao achei' no CRM.")
    return True


def linha_da_pessoa(page: Page, pessoa: Pessoa):
    """Acha a linha pelo CPF, ou None.

    Nunca cai para a posicao na tabela: com varios operadores em paralelo, uma
    linha some de Pendentes a qualquer momento e todas as outras sobem. Escrever
    por indice defasado gravaria no cadastro errado, em silencio.
    """
    rows = page.locator("table tbody tr")
    cpf_idx = header_indexes(page).get(normalize_header("CPF"))
    if cpf_idx is None:
        print("Nao identifiquei a coluna de CPF na tabela do CRM.")
        return None

    for idx in range(rows.count()):
        cells = rows.nth(idx).locator("td")
        if cpf_idx >= cells.count():
            continue
        try:
            if only_digits(cells.nth(cpf_idx).inner_text(timeout=5000)) == pessoa.cpf:
                return rows.nth(idx)
        except (TimeoutError, Error):
            continue
    return None


def atualizar_crm(page: Page, pessoa: Pessoa, texto_resultado: str) -> bool:
    # Localiza pelo CPF, nunca pela posicao: com varios operadores a tabela se
    # reordena durante a consulta ao TSE e o indice apontaria para outra pessoa.
    row = linha_da_pessoa(page, pessoa)
    if row is None:
        print(f"Nao localizei a linha de {pessoa.nome} para atualizar. Nada foi gravado.")
        return False

    row.get_by_role("button", name=re.compile(r"Atualizar", re.I)).click(timeout=15000)

    try:
        page.get_by_text(re.compile(r"Atualizar local", re.I)).first.wait_for(timeout=10000)
    except TimeoutError:
        pass

    textarea = page.locator("textarea").last
    textarea.fill(texto_resultado, timeout=15000)
    page.get_by_role("button", name=re.compile(r"Salvar local", re.I)).click(timeout=15000)

    try:
        page.get_by_role("button", name=re.compile(r"Salvar local", re.I)).wait_for(state="hidden", timeout=15000)
        return True
    except TimeoutError:
        print("Cliquei em salvar, mas o modal nao fechou dentro do tempo esperado. Confira no navegador.")
        click_if_visible(page, page.get_by_role("button", name=re.compile(r"Cancelar|Fechar", re.I)).last)
        return False


def motivo_inativacao(resultado: ResultadoTse) -> str:
    status = normalize_text(resultado.status)
    comunicado = normalize_text(resultado.comunicado)
    texto = normalize_text(resultado.texto_para_crm)
    blob = " ".join((status, comunicado, texto))

    # blob ja vem sem acento e em caixa alta (normalize_text), por isso os termos aqui
    # tambem sao escritos sem acento.
    if any(term in blob for term in ("CANCELADO", "CANCELAMENTO", "SUSPENSO", "SUSPENSA", "REVISAO DE ELEITORADO")):
        return "Título cancelado"

    if any(term in blob for term in ("NAO QUITE", "QUITACAO ELEITORAL PENDENTE", "EM DEBITO COM A JUSTICA ELEITORAL")):
        return "Não quite"

    # Biometria nao coletada ou desatualizada NAO inativa: o titulo segue
    # regular e a pessoa vota normalmente. O comunicado continua registrado no
    # CSV para quem quiser acompanhar, mas o cadastro fica ativo.

    if any(term in blob for term in ("INVALIDO", "INEXISTENTE")):
        return "Dados inválidos"

    return ""


def inativar_cadastro_validado(page: Page, pessoa: Pessoa, motivo: str) -> None:
    print(f"Inativando cadastro validado por motivo: {motivo}")
    page.get_by_role("button", name=re.compile(r"Já validados|Ja validados", re.I)).click(timeout=15000)
    page.wait_for_timeout(1200)
    fill_search(page, pessoa.cpf)

    row = wait_filtered_row(page)
    buttons = row.get_by_role("button")
    if buttons.count() == 0:
        print("Nao encontrei botao de acao para inativar. Confira manualmente no CRM.")
        return

    buttons.last.click(timeout=15000)
    try:
        page.get_by_text(re.compile(r"Inativar cadastro", re.I)).first.wait_for(timeout=10000)
    except TimeoutError:
        pass

    select = page.locator("select").last
    if not escolher_motivo(select, motivo):
        print(f"Nao consegui selecionar o motivo '{motivo}' no CRM. Nao vou salvar a inativacao desta pessoa.")
        click_if_visible(page, page.get_by_role("button", name=re.compile(r"Cancelar|Fechar", re.I)).last)
        return

    save = page.get_by_role("button", name=re.compile(r"Salvar|Inativar|Confirmar", re.I)).last
    save.click(timeout=15000)
    try:
        page.get_by_text(re.compile(r"Inativar cadastro", re.I)).first.wait_for(state="hidden", timeout=15000)
    except TimeoutError:
        print("Cliquei para salvar a inativacao, mas o modal nao fechou dentro do tempo esperado. Confira no navegador.")
    finally:
        voltar_para_pendentes(page)


def escolher_motivo(select, motivo: str) -> bool:
    """Casa o motivo com as opcoes que o CRM realmente oferece.

    O rotulo exato varia entre telas ("Nao quite", "Nao quite com a Justica
    Eleitoral"...), entao compara sem acento/caixa e aceita correspondencia
    parcial antes de desistir. Nunca escolhe uma opcao arbitraria: se nada casar,
    devolve False e quem chamou cancela o modal.
    """
    opcoes = []
    for option in select.locator("option").all():
        try:
            rotulo = clean(option.inner_text(timeout=5000))
        except (TimeoutError, Error):
            continue
        valor = option.get_attribute("value", timeout=2000) or ""
        if rotulo:
            opcoes.append((rotulo, valor))

    if not opcoes:
        print("O select de motivo veio vazio.")
        return False

    candidatos = [motivo, *MOTIVO_ALTERNATIVAS.get(motivo, ())]
    alvos = [normalize_text(c) for c in candidatos if c]

    # 1a passada: rotulo identico. 2a: um contido no outro.
    for comparar in (
        lambda rotulo, alvo: rotulo == alvo,
        lambda rotulo, alvo: alvo in rotulo or rotulo in alvo,
    ):
        for alvo in alvos:
            for rotulo, valor in opcoes:
                if not comparar(normalize_text(rotulo), alvo):
                    continue
                try:
                    select.select_option(label=rotulo, timeout=10000)
                except Error:
                    if not valor:
                        continue
                    select.select_option(value=valor, timeout=10000)
                print(f"Motivo selecionado no CRM: {rotulo}")
                return True

    # Ultimo recurso: cai em "Outro". Perde granularidade, mas o cadastro fica
    # inativado em vez de passar batido.
    for rotulo, valor in opcoes:
        if normalize_text(rotulo) in MOTIVO_GENERICO:
            try:
                select.select_option(label=rotulo, timeout=10000)
            except Error:
                if not valor:
                    continue
                select.select_option(value=valor, timeout=10000)
            print(f"Nenhuma opcao especifica para '{motivo}'. Usei '{rotulo}'.")
            return True

    print(f"Nenhuma opcao do CRM corresponde a '{motivo}'. Opcoes disponiveis: {[r for r, _ in opcoes]}")
    return False


def fill_search(page: Page, value: str) -> None:
    search = page.get_by_placeholder(re.compile(r"Buscar|CPF|nome", re.I)).first
    search.fill(value, timeout=15000)
    page.wait_for_timeout(1200)


def wait_filtered_row(page: Page):
    rows = page.locator("table tbody tr")
    rows.first.wait_for(timeout=15000)
    if rows.count() > 1:
        page.wait_for_timeout(1200)
    return rows.first


def fill_first_placeholder(page: Page, patterns: Iterable[str], value: str) -> None:
    for pattern in patterns:
        locator = page.get_by_placeholder(re.compile(pattern, re.I)).first
        try:
            locator.fill(value, timeout=8000)
            return
        except (TimeoutError, Error):
            continue
    raise RuntimeError(f"Nao encontrei campo para preencher: {patterns}")


def fill_first_locator(page: Page, selectors: Iterable[str], value: str) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.fill(value, timeout=8000)
            return
        except (TimeoutError, Error):
            continue
    raise RuntimeError(f"Nao encontrei campo para preencher: {tuple(selectors)}")


def extract_label_value(texto: str, label: str) -> str:
    escaped = re.escape(label)
    match = re.search(rf"{escaped}\s*\n\s*([^\n]+)", texto, flags=re.I)
    return clean(match.group(1)) if match else ""


def extract_status(texto: str) -> str:
    match = re.search(r"Seu título.*?(?=\n\n|Este é|Comunicado|$)", texto, flags=re.I | re.S)
    if match:
        return clean(match.group(0))
    match = re.search(r"Seu titulo.*?(?=\n\n|Este e|Comunicado|$)", texto, flags=re.I | re.S)
    return clean(match.group(0)) if match else ""


def extract_comunicado(texto: str) -> str:
    match = re.search(r"Comunicado\s*(.*?)(?:\n\s*Voltar\b|$)", texto, flags=re.I | re.S)
    return clean(match.group(1)) if match else ""


def extract_negative_message(texto: str) -> str:
    lines = [clean(line) for line in texto.splitlines() if clean(line)]
    for index, line in enumerate(lines):
        normalized = normalize_text(line)
        if any(term in normalized for term in ("NAO CONFEREM", "NAO FOI POSSIVEL", "NAO ENCONTR", "VERIFIQUE OS DADOS", "INVALID")):
            return clean(" ".join(lines[index : index + 3]))
    return ""


def is_irregular(status: str, comunicado: str, texto: str) -> bool:
    blob = normalize_text(" ".join((status, comunicado, texto[:1200])))
    return any(term in blob for term in IRREGULAR_TERMS)


def append_log(pessoa: Pessoa, resultado: ResultadoTse) -> None:
    ensure_log_header()
    is_new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if is_new:
            writer.writerow(LOG_HEADER)
        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                pessoa.nome,
                pessoa.cpf,
                pessoa.mae,
                pessoa.nascimento,
                "sim" if resultado.encontrado else "nao",
                "sim" if resultado.irregular else "nao",
                resultado.status,
                resultado.comunicado,
                resultado.texto_para_crm,
            ]
        )
    append_detail_log(pessoa, resultado)


def append_detail_log(pessoa: Pessoa, resultado: ResultadoTse) -> None:
    """Mantem o resultado legivel fora do terminal sem afetar a consulta."""
    try:
        with DETAIL_LOG.open("a", encoding="utf-8") as file:
            file.write("\n" + "=" * 78 + "\n")
            file.write(f"{datetime.now().isoformat(timespec='seconds')} | {pessoa.nome} | CPF {pessoa.cpf}\n")
            file.write(resultado.texto_para_crm.rstrip() + "\n")
    except OSError:
        # O CSV continua sendo a fonte principal; falha no TXT nao interrompe a fila.
        pass


def ensure_log_header() -> None:
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
        return

    with LOG_FILE.open("r", newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        current_header = next(reader, [])

    if current_header == LOG_HEADER:
        return

    backup = LOG_FILE.with_name(f"{LOG_FILE.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{LOG_FILE.suffix}")
    LOG_FILE.rename(backup)
    print(f"CSV antigo tinha outro formato. Fiz backup em: {backup}")


def add_field(lines: list[str], label: str, value: str) -> None:
    if value:
        lines.append(f"{label}: {value}")


def only_digits(value: str) -> str:
    return re.sub(r"\D+", "", value)


def clean(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\r", "")).strip()


def campo_sem_informacao(value: str) -> bool:
    """Reconhece celula vazia e marcadores comuns usados pelo CRM."""
    normalizado = normalize_text(value)
    if normalizado in {
        "",
        "-",
        "--",
        "NAO INFORMADO",
        "NAO INFORMADA",
        "SEM INFORMACAO",
        "NAO CADASTRADO",
        "NAO CADASTRADA",
    }:
        return True
    return any(
        marcador in normalizado
        for marcador in (
            "SEM REGISTRO",
            "NAO REGISTRAD",
            "NAO CONSTA",
            "SEM CADASTRO",
            "NAO DECLARAD",
        )
    )


def clean_person_name(value: str) -> str:
    value = re.sub(r"^[^\wÀ-ÿ]+", "", value)
    value = re.sub(r"^(?:⏳|✅|☑|✓|✔)\s*", "", value)
    return clean(value)


def normalize_header(value: str) -> str:
    value = re.sub(r"[↕↑↓▲△▴▾]+", "", value)
    return normalize_text(value)


def normalize_text(value: str) -> str:
    replacements = str.maketrans(
        "áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ",
        "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
    )
    return clean(value).translate(replacements).upper()


def ask_user(message: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        print(message)
        print("Nao ha terminal interativo para responder. Execute pelo terminal comum para continuar manualmente.")
        raise RuntimeError("Execucao sem terminal interativo.")

    try:
        return input(message)
    except EOFError as exc:
        raise RuntimeError("Nao consegui ler sua resposta no terminal. Rode o script em um terminal interativo.") from exc


def ask_user_timeout(message: str, timeout_s: float, default: str = "") -> str:
    """Le uma resposta sem permitir que uma pessoa pare a rodada para sempre."""
    if not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError("Execucao sem terminal interativo.")

    if sys.platform.startswith("win"):
        import msvcrt

        sys.stdout.write(message)
        sys.stdout.flush()
        caracteres: list[str] = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            caractere = msvcrt.getwch()
            if caractere in ("\r", "\n"):
                print()
                return "".join(caracteres)
            if caractere == "\x03":
                raise KeyboardInterrupt
            if caractere in ("\x00", "\xe0"):
                msvcrt.getwch()
                continue
            if caractere == "\b":
                if caracteres:
                    caracteres.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            caracteres.append(caractere)
            sys.stdout.write(caractere)
            sys.stdout.flush()
    else:
        import select

        sys.stdout.write(message)
        sys.stdout.flush()
        pronto, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if pronto:
            return sys.stdin.readline().rstrip("\r\n")

    print(f"\nTempo de resposta manual esgotado ({int(timeout_s)}s). Seguindo com a opcao segura.")
    return default


def ask_multiline_until_end(timeout_s: float | None = None) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError("Execucao sem terminal interativo.")

    lines: list[str] = []
    deadline = time.monotonic() + timeout_s if timeout_s is not None else None
    while True:
        if deadline is None:
            try:
                line = input()
            except EOFError as exc:
                raise RuntimeError("Nao consegui ler o texto colado no terminal.") from exc
        else:
            restante = deadline - time.monotonic()
            if restante <= 0:
                print("Tempo para colar o resultado esgotado. A pessoa fica para o repasse.")
                return "PULAR"
            line = ask_user_timeout("", restante, default="PULAR")

        if normalize_text(line) == "FIM":
            break
        if not lines and normalize_text(line) == "PULAR":
            return "PULAR"
        lines.append(line)

    return "\n".join(lines)


def pausar_antes_de_fechar() -> None:
    """Segura a janela aberta. Clicando no .exe, ela fecharia sozinha e o
    operador nao leria mensagem nenhuma."""
    if not getattr(sys, "frozen", False):
        return
    try:
        input("\nPressione Enter para fechar esta janela...")
    except (EOFError, KeyboardInterrupt):
        pass


def banner() -> None:
    print("=" * 64)
    print(" CRM x TSE - consulta de local de votacao")
    print("=" * 64)
    print(f"Os arquivos ficam em: {BASE_DIR}")
    print("  consultas.csv  - registro de tudo que foi consultado")
    print("  bot_error.log  - detalhe do ultimo erro, se houver")
    print("  execucao_detalhada.txt - resultados completos sem lotar o terminal")
    print()
    print("Requisitos: Google Chrome instalado e internet.")
    print("Voce vai precisar resolver o CAPTCHA do TSE a cada consulta.")
    print("Para interromper a qualquer momento: feche esta janela ou Ctrl+C.")
    print("=" * 64)
    print()


def autoteste() -> int:
    """Checagem rapida do ambiente, sem tocar no CRM nem no TSE.

    Serve de diagnostico quando o operador diz "nao abre": separa problema de
    Chrome ausente de problema de empacotamento do Playwright.
    """
    print("1/3 Procurando o Google Chrome...")
    try:
        chrome = find_chrome_executable()
    except RuntimeError as exc:
        print(f"    FALHOU: {exc}")
        return 1
    print(f"    ok: {chrome}")

    print("2/3 Iniciando o Playwright...")
    try:
        with sync_playwright() as playwright:
            print("    ok")
            print("3/3 Abrindo o Chrome...")
            navegador = playwright.chromium.launch(executable_path=chrome, headless=True)
            pagina = navegador.new_page()
            pagina.goto("about:blank")
            print(f"    ok: Chrome {navegador.version}")
            navegador.close()
    except Exception as exc:
        print(f"    FALHOU: {type(exc).__name__}: {exc}")
        return 1

    print(f"\nTudo certo. Os arquivos serao gravados em: {BASE_DIR}")
    return 0


if __name__ == "__main__":
    try:
        banner()
        if "--teste" in sys.argv:
            # o pause vem do finally, nao repetir aqui
            raise SystemExit(autoteste())
        main()
        print("\nConcluido.")
    except KeyboardInterrupt:
        print("\nInterrompido por voce. O que ja foi salvo continua no CRM e no CSV.")
    except Exception:
        # Append, nao write: os erros de cada pessoa ja foram acumulados aqui
        # durante a execucao e sobrescrever apagaria o historico.
        with ERROR_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"\n{'=' * 70}\n{datetime.now().isoformat(timespec='seconds')} | erro geral\n")
            arquivo.write(traceback.format_exc())
        print(f"\nDeu erro. Salvei o detalhe em: {ERROR_LOG}")
        print("Ultimas linhas do erro:")
        print("\n".join(traceback.format_exc().splitlines()[-8:]))
        if not getattr(sys, "frozen", False):
            raise
    finally:
        encerrar_sessao_tse()
        pausar_antes_de_fechar()
