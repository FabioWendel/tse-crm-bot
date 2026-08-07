from __future__ import annotations

import csv
import re
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

PROFILE_DIR = Path(__file__).with_name(".browser-profile")
TSE_PROFILE_DIR = Path(__file__).with_name(".tse-chrome-profile")
LOG_FILE = Path(__file__).with_name("consultas.csv")
ERROR_LOG = Path(__file__).with_name("bot_error.log")
LOG_HEADER = ["data_hora", "nome", "cpf", "mae", "nascimento", "encontrado", "irregular", "status", "comunicado", "resultado"]
CHROME_EXECUTABLE = "/usr/bin/google-chrome"
TSE_REMOTE_DEBUGGING_PORT = 9222

HEADLESS = False
SLOW_MO_MS = 120
MAX_PESSOAS = 0  # 0 = processa todas as linhas visiveis da pagina atual
TSE_RESPONSE_TIMEOUT_MS = 90000
MAX_TSE_ATTEMPTS = 3
TSE_NO_CHROME_NORMAL = True  # usa Chrome separado, preenche a consulta e deixa CAPTCHA manual

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


def main() -> None:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            executable_path=CHROME_EXECUTABLE,
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
            viewport={"width": 1366, "height": 768},
            args=["--start-maximized"],
        )

        crm = context.pages[0] if context.pages else context.new_page()
        crm.goto(CRM_URL, wait_until="domcontentloaded")
        ensure_crm_ready(crm)

        total = total_rows(crm)
        if total == 0:
            print("Nao encontrei linhas na tabela do CRM.")
            context.close()
            return

        limite = total if MAX_PESSOAS <= 0 else min(MAX_PESSOAS, total)
        print(f"Encontrei {total} linha(s). Vou processar {limite}.")

        processados: set[str] = set()
        for index in range(limite):
            voltar_para_pendentes(crm)
            pessoa = read_next_pending_person(crm, processados)
            if not pessoa:
                print("Nao encontrei outra pessoa pendente legivel na tela atual.")
                break

            processados.add(pessoa.cpf)

            print(f"\n[{index + 1}/{limite}] Consultando {pessoa.nome} - CPF {pessoa.cpf}")
            resultado = consultar_tse(playwright, context, pessoa)

            append_log(pessoa, resultado)

            if not resultado.encontrado:
                print("Nao encontrei local de votacao para esta pessoa. Gravei como ERRO NAO IDENTIFICADO no CSV e vou pular o CRM.")
                fechar_tse_resultado(resultado)
                voltar_para_pendentes(crm)
                continue

            if resultado.irregular:
                print("\nATENCAO: encontrei situacao/comunicado que precisa da sua decisao.")
                print(f"Pessoa: {pessoa.nome} - CPF {pessoa.cpf}")
                print(f"Status: {resultado.status or 'nao identificado'}")
                if resultado.comunicado:
                    print(f"Comunicado: {resultado.comunicado}")
                print("Como existe local de votacao, ele sera atualizado no CRM apos sua confirmacao.")
                confirm = ask_user("Confira visualmente o resultado no TSE. Digite S para salvar no CRM, ou qualquer outra tecla para pular: ").strip().upper()
                if confirm != "S":
                    print("Pulando salvamento desta pessoa.")
                    fechar_tse_resultado(resultado)
                    voltar_para_pendentes(crm)
                    continue
            else:
                print("Pessoa regular sem alerta. Salvando automaticamente no CRM.")

            atualizar_crm(crm, pessoa.row_index, resultado.texto_para_crm)
            motivo = motivo_inativacao(resultado)
            if motivo:
                try:
                    inativar_cadastro_validado(crm, pessoa, motivo)
                except Exception:
                    ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
                    print(f"Nao consegui inativar automaticamente. Salvei o detalhe em: {ERROR_LOG}")
                    print("Confira/inative manualmente no CRM antes de seguir.")
            fechar_tse_resultado(resultado)
            voltar_para_pendentes(crm)
            if resultado.irregular:
                ask_user("Salvei no CRM e fechei o TSE. Pressione Enter para ir para a proxima pessoa...")
            else:
                print("Salvei no CRM e fechei o TSE. Indo para a proxima pessoa...")

        print("\nProcesso finalizado para as linhas visiveis.")
        context.close()


def ensure_crm_ready(page: Page) -> None:
    try:
        page.get_by_role("button", name=re.compile(r"Atualizar", re.I)).first.wait_for(timeout=8000)
        return
    except TimeoutError:
        pass

    print("O CRM ainda nao mostrou a tabela. Se estiver na tela de login, faca login no navegador.")
    ask_user("Depois que a tabela aparecer, pressione Enter aqui...")
    page.goto(CRM_URL, wait_until="domcontentloaded")
    page.get_by_role("button", name=re.compile(r"Atualizar", re.I)).first.wait_for(timeout=30000)


def total_rows(page: Page) -> int:
    return page.locator("table tbody tr").count()


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

    if not nome or not cpf or not mae or not nascimento:
        print(f"Dados incompletos na linha {row_index + 1}: nome={nome!r}, cpf={cpf!r}, mae={mae!r}, nasc={nascimento!r}")
        return None

    return Pessoa(row_index=row_index, nome=nome, cpf=cpf, mae=mae, nascimento=nascimento)


def read_next_pending_person(page: Page, processed_cpfs: set[str]) -> Pessoa | None:
    rows_count = total_rows(page)
    for row_index in range(rows_count):
        pessoa = read_person_from_row(page, row_index)
        if pessoa and pessoa.cpf not in processed_cpfs:
            return pessoa
    return None


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

    for tentativa in range(1, MAX_TSE_ATTEMPTS + 1):
        print(f"Tentativa TSE {tentativa}/{MAX_TSE_ATTEMPTS}")
        page.goto(TSE_URL, wait_until="domcontentloaded")
        abrir_onde_votar(page)
        if not preencher_autenticacao(page, pessoa):
            if tentativa < MAX_TSE_ATTEMPTS:
                print("Tela do TSE ainda carregando/travada. Vou tentar esta pessoa novamente.")
                continue
            return resultado_sem_identificacao("ERRO NAO IDENTIFICADO", "Tela do TSE ficou carregando e nao permitiu clicar em Entrar.")

        if esperar_resultado_ou_acao_manual(page):
            break

        if tentativa < MAX_TSE_ATTEMPTS:
            limpar_erro_captcha(page)
            print("Vou tentar a mesma pessoa novamente.")
            continue

        return consultar_tse_manual(
            pessoa,
            "Nao veio resposta do TSE apos varias tentativas ou o CAPTCHA ficou indisponivel.",
        )

    texto = clean(page.locator("body").inner_text(timeout=30000))
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

    print("Resultado TSE:")
    print(texto_para_crm)
    return ResultadoTse(
        texto_para_crm=texto_para_crm,
        status=status,
        comunicado=comunicado,
        irregular=irregular,
        encontrado=encontrado,
    )


def consultar_tse_chrome_normal(playwright, pessoa: Pessoa) -> ResultadoTse:
    chrome_process = abrir_tse_no_chrome_normal()
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{TSE_REMOTE_DEBUGGING_PORT}")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[-1] if context.pages else context.new_page()
    page.goto(TSE_URL, wait_until="domcontentloaded")
    resultado = consultar_tse_playwright_page(page, pessoa)

    def fechar() -> None:
        try:
            browser.close()
        except Error:
            pass
        if chrome_process:
            try:
                chrome_process.terminate()
            except OSError:
                pass

    resultado.fechar_tse = fechar
    return resultado


def abrir_onde_votar(page: Page) -> None:
    wait_tse_loading(page)
    try:
        page.get_by_text(re.compile(r"onde votar", re.I)).first.click(timeout=8000)
    except (TimeoutError, Error):
        pass

    wait_tse_loading(page)
    try:
        page.locator("#titulo-cpf-nome").or_(page.get_by_role("textbox", name=re.compile(r"título eleitoral|titulo eleitoral|CPF", re.I))).first.wait_for(timeout=15000)
    except TimeoutError:
        ask_user("Nao achei a tela de autenticacao do TSE. Abra 'Onde Votar' manualmente e pressione Enter...")


def preencher_autenticacao(page: Page, pessoa: Pessoa) -> bool:
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
        page.get_by_role("button", name=re.compile(r"Entrar", re.I)).click(timeout=20000)
        return True
    except TimeoutError:
        wait_tse_loading(page, timeout_ms=30000)
        try:
            page.get_by_role("button", name=re.compile(r"Entrar", re.I)).click(timeout=10000)
            return True
        except TimeoutError:
            return False


def esperar_resultado_ou_acao_manual(page: Page) -> bool:
    print("Aguardando resposta do TSE...")
    deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)

    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        texto = page.locator("body").inner_text(timeout=10000)
        if has_captcha_error(texto):
            limpar_erro_captcha(page)
            return False

        if has_voting_place_result(texto) or has_tse_negative_response(texto):
            return True

        if has_captcha(page):
            resposta = ask_user("O TSE pediu validacao de robo. Resolva no navegador e pressione Enter aqui; digite R se errou e quer reiniciar esta tentativa: ").strip().upper()
            if resposta == "R":
                return False
            deadline = time.monotonic() + (TSE_RESPONSE_TIMEOUT_MS / 1000)

    resposta = ask_user("Ainda nao identifiquei o resultado/modal do TSE. Pressione Enter para eu ler agora, ou digite R para tentar de novo esta pessoa: ").strip().upper()
    if resposta == "R":
        return False

    texto = page.locator("body").inner_text(timeout=10000)
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


def has_captcha_error(texto: str) -> bool:
    normalized = normalize_text(texto)
    return any(term in normalized for term in CAPTCHA_ERROR_TERMS)


def limpar_erro_captcha(page: Page) -> None:
    print("Detectei erro/indisponibilidade de CAPTCHA. Limpando tela para tentar de novo...")
    click_if_visible(page, page.get_by_role("button", name=re.compile(r"Fechar", re.I)).first)
    click_if_visible(page, page.get_by_role("button", name=re.compile(r"Tentar novamente", re.I)).first)
    click_if_visible(page, page.locator("button, [role='button']").filter(has_text=re.compile(r"×|x", re.I)).first)
    page.wait_for_timeout(1200)
    wait_tse_loading(page, timeout_ms=20000)


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
    return ResultadoTse(
        texto_para_crm=f"{status}: {comunicado}",
        status=status,
        comunicado=comunicado,
        irregular=False,
        encontrado=False,
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

    pasted = ask_multiline_until_end()
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
    try:
        process = subprocess.Popen(
            [
                CHROME_EXECUTABLE,
                f"--remote-debugging-port={TSE_REMOTE_DEBUGGING_PORT}",
                f"--user-data-dir={TSE_PROFILE_DIR}",
                "--no-first-run",
                "--new-window",
                TSE_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        wait_cdp_ready()
        return process
    except OSError:
        print(f"Nao consegui abrir {CHROME_EXECUTABLE}. Abra manualmente: {TSE_URL}")
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
    raise RuntimeError("Chrome normal abriu, mas a porta de controle nao respondeu.")


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


def atualizar_crm(page: Page, row_index: int, texto_resultado: str) -> None:
    row = page.locator("table tbody tr").nth(row_index)
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
    except TimeoutError:
        print("Cliquei em salvar, mas o modal nao fechou dentro do tempo esperado. Confira no navegador.")


def motivo_inativacao(resultado: ResultadoTse) -> str:
    status = normalize_text(resultado.status)
    comunicado = normalize_text(resultado.comunicado)
    texto = normalize_text(resultado.texto_para_crm)
    blob = " ".join((status, comunicado, texto))

    if any(term in blob for term in ("CANCELADO", "CANCELAMENTO", "SUSPENSO", "SUSPENSA", "REVISAO DE ELEITORADO")):
        return "Título cancelado"

    if "BIOMETRIA" in blob and any(term in blob for term in ("NAO COLETADA", "NÃO COLETADA", "NAO COLETADO", "NÃO COLETADO")):
        return "Problema na biometria"

    if any(term in blob for term in ("INVALIDO", "INVÁLIDO", "INEXISTENTE")):
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
    try:
        select.select_option(label=motivo, timeout=10000)
    except Error:
        select.select_option(value=motivo, timeout=10000)

    save = page.get_by_role("button", name=re.compile(r"Salvar|Inativar|Confirmar", re.I)).last
    save.click(timeout=15000)
    try:
        page.get_by_text(re.compile(r"Inativar cadastro", re.I)).first.wait_for(state="hidden", timeout=15000)
    except TimeoutError:
        print("Cliquei para salvar a inativacao, mas o modal nao fechou dentro do tempo esperado. Confira no navegador.")
    finally:
        voltar_para_pendentes(page)


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


def ask_multiline_until_end() -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError("Execucao sem terminal interativo.")

    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError as exc:
            raise RuntimeError("Nao consegui ler o texto colado no terminal.") from exc

        if normalize_text(line) == "FIM":
            break
        if not lines and normalize_text(line) == "PULAR":
            return "PULAR"
        lines.append(line)

    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"\nDeu erro. Salvei o detalhe em: {ERROR_LOG}")
        print("Ultimas linhas do erro:")
        print("\n".join(ERROR_LOG.read_text(encoding="utf-8").splitlines()[-12:]))
        raise
