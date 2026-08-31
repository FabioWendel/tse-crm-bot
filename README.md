# Robô CRM + TSE

Automação em Python/Playwright para consultar "Onde Votar" no autoatendimento
do TSE e gravar o resultado no CRM.

> Para quem só vai **operar** (não desenvolver), veja
> [LEIA-ME-OPERADOR.md](LEIA-ME-OPERADOR.md) e use o executável pronto.

## O que ele faz

1. Pergunta qual operador é este, quantos vão rodar em paralelo e o teto de
   CPFs da rodada.
2. Inventaria **todas as páginas** de "Pendentes" no CRM e separa a fatia do
   operador por `sha1(cpf) % total`.
3. Para cada pessoa: busca pelo CPF, lê os dados e consulta o TSE num Chrome
   separado.
4. Espera você resolver o CAPTCHA. **O robô não tenta burlá-lo.**
5. Grava o local de votação no CRM, ou marca "Não achei" quando o TSE responde
   que não localizou a pessoa.
6. Inativa o cadastro quando aplicável: `Título cancelado`, `Não quite`,
   `Problema na biometria`, `Dados inválidos` — caindo em "Outro" se o CRM não
   oferecer a opção específica.

Situação irregular **com** local de votação é salva automaticamente. Para voltar
a pedir confirmação no terminal, ponha `CONFIRMAR_IRREGULAR = True`.

## Execução paralela

A fatia sai de `sha1(cpf) % total`, então os operadores não precisam se
coordenar em tempo real — a divisão é idêntica em toda máquina e estável entre
execuções. Basta todos informarem **o mesmo total** e **números diferentes**.

⚠️ Só as fatias que rodarem são processadas. Combinar 10 e rodar 3 deixa ~70%
da fila sem tratamento.

Toda escrita no CRM localiza a linha **pelo CPF**, nunca pela posição na
tabela: com vários operadores, uma linha sai de "Pendentes" a qualquer momento
e as outras sobem — gravar por índice defasado escreveria no cadastro errado.

## Desenvolvimento

Pré-requisitos: Python 3.11+ e **Google Chrome instalado**. Não é preciso
`playwright install`: o bot dirige o Chrome do sistema, não o Chromium do
Playwright.

### Abrir a versão normal com Brave no TSE

Use `INICIAR-BRAVE.cmd`. O CRM continua no Google Chrome e somente a janela do
TSE abre no Brave instalado. Essa opção usa perfil persistente exclusivo
`.tse-brave-profile/` e porta local 9226; não reutiliza o perfil pessoal do
Brave e não ativa stealth, proxy ou resolução automática de CAPTCHA.

Durante uma rodada, a mesma janela do TSE permanece aberta e é reutilizada para
todas as pessoas. Antes de uma nova consulta, o programa tenta acionar **Não sou
este eleitor** e confirma que o perfil anterior desapareceu. O processo fecha
somente quando o programa termina ou é interrompido, evitando deixar a porta de
controle ocupada para a próxima execução.

### Linux/macOS

```bash
git clone https://github.com/FabioWendel/tse-crm-bot.git
cd tse-crm-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python crm_tse_bot.py
```

### Windows PowerShell

```powershell
git clone https://github.com/FabioWendel/tse-crm-bot.git
cd tse-crm-bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python crm_tse_bot.py
```

Se o PowerShell bloquear a ativação da venv, rode uma vez:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Ou dispense a ativação chamando o interpretador da venv direto:

```powershell
.\.venv\Scripts\python.exe crm_tse_bot.py
```

### Diagnóstico

```bash
python crm_tse_bot.py --teste
```

Checa Chrome → Playwright → abertura do navegador, sem tocar em CRM nem TSE.

## Gerando o pacote para os operadores

**PyInstaller não faz compilação cruzada.** O pacote de cada sistema tem de ser
gerado naquele sistema: `.exe` no Windows, `.dmg` no macOS. Não há como
produzir um a partir do outro.

O `build.py` detecta a plataforma e faz o que couber.

### Windows → `dist\CRM-TSE-Bot.exe`

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe build.py
```

Sai um executável único de ~45 MB. A máquina do operador não precisa de
Python, venv nem pip — só do Chrome.

> Se o build falhar com `PermissionError` em `dist\`, é o próprio `.exe`
> aberto. Feche-o e rode de novo.

### macOS → `dist/CRM-TSE-Bot.dmg`

```bash
./.venv/bin/python -m pip install pyinstaller
./.venv/bin/python build.py
```

O `.dmg` sai com o binário, um `CRM-TSE-Bot.command` clicável (abre o Terminal,
onde o operador digita os números) e o LEIA-ME.

O binário **não é assinado**, então o Gatekeeper bloqueia no primeiro uso. O
operador precisa clicar com o botão direito no `.command` → **Abrir**, ou rodar:

```bash
xattr -dr com.apple.quarantine /caminho/para/CRM-TSE-Bot
```

Assinar de verdade exige conta paga de desenvolvedor Apple.

> O caminho do macOS ainda **não foi executado por ninguém** — foi escrito e
> revisado, mas a máquina de desenvolvimento é Windows. Trate a primeira
> geração do `.dmg` como validação.

## Relatório e separação de listas

Analisa o `consultas.csv` e separa as pessoas por situação. Roda em **qualquer
máquina** que tenha o repositório e um `consultas.csv` — não acessa CRM, TSE
nem rede.

```bash
python relatorio_consultas.py                  # usa ./consultas.csv
python relatorio_consultas.py outra/pasta.csv  # usa outro arquivo
```

Ele importa as regras do próprio `crm_tse_bot`, então a classificação nunca
diverge do que o robô faria hoje. Gera três arquivos ao lado do CSV de origem:

| Arquivo | Conteúdo |
|---|---|
| `consultas-reativar-biometria.csv` | inativados pela regra antiga de biometria, que hoje devem ficar **ativos** |
| `consultas-inativar.csv` | quem as regras atuais mandam inativar |
| `consultas-nao-encontrados.csv` | quem o TSE não localizou |

Quando a mesma pessoa aparece mais de uma vez no CSV, vale a consulta mais
recente.

> Os três saem no padrão `consultas*.csv`, coberto pelo `.gitignore`. Têm CPF e
> nome: não versione nem compartilhe fora do combinado.

### Sobre a lista de reativação

Biometria não coletada ou desatualizada **deixou de inativar** o cadastro — o
título segue regular e a pessoa vota normalmente. Quem já tinha sido inativado
por essa regra precisa voltar a ativo, e é isso que a lista separa.

A reativação é manual, pela tela do CRM: o bot só sabe inativar. Fazer por
`UPDATE` direto no banco esbarra na constraint `chk_pessoa_ativa_whatsapp`
(pessoa ativa exige WhatsApp normalizado) e pula a trilha de auditoria que o
CRM mantém.

## Conversor de CSV para Excel

O `consultas.csv` é UTF-8, separado por vírgula e tem campos multilinha — abrir
com duplo clique no Excel em português embaralha tudo. Para converter:

```bash
pip install openpyxl
python csv_para_xlsx.py
```

Sai um `.xlsx` em modo tabela, com filtro e cabeçalho congelado.

## Dados pessoais

`consultas.csv` e `consultas.xlsx` contêm CPF, nome da mãe e endereço de
eleitores. Estão no `.gitignore` e **não devem ser versionados nem
compartilhados** fora do combinado com o responsável.

## Ajustes

As constantes no começo de `crm_tse_bot.py` controlam URLs, tempo de espera,
teto padrão da rodada (`LIMITE_PADRAO`), confirmação de irregulares
(`CONFIRMAR_IRREGULAR`) e o que conta como situação irregular.
