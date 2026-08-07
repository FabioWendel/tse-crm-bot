# Robô CRM + TSE

Automação em Python/Playwright para:

1. Ler nome, CPF, nome da mãe e nascimento no CRM.
2. Consultar "Onde Votar" no autoatendimento do TSE em um Chrome separado.
3. Esperar sua ação quando aparecer validação de robô/CAPTCHA.
4. Salvar automaticamente no CRM quando o título estiver regular e sem alerta.
5. Pedir confirmação quando houver título cancelado, suspenso, inválido, biometria não coletada ou outro alerta.
6. Abrir o modal "Atualizar local de votação" no CRM, colar o resultado e salvar.
7. Inativar automaticamente cadastros já validados quando aplicável:
   - `Título cancelado`
   - `Problema na biometria`
   - `Dados inválidos`

## Instalação

### Linux/macOS

```bash
git clone https://github.com/FabioWendel/tse-crm-bot.git
cd tse-crm-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### Windows PowerShell

Instale o Python 3 e o Google Chrome antes.

```powershell
git clone https://github.com/FabioWendel/tse-crm-bot.git
cd tse-crm-bot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Se o PowerShell bloquear a ativação da venv, rode uma vez:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Uso

Linux/macOS:

```bash
cd tse-crm-bot
source .venv/bin/activate
python crm_tse_bot.py
```

Windows PowerShell:

```powershell
cd tse-crm-bot
.\.venv\Scripts\Activate.ps1
python crm_tse_bot.py
```

Na primeira execução, faça login no CRM na janela que abrir. Depois volte ao terminal e pressione `Enter`.

O robô não tenta burlar CAPTCHA. Quando o TSE pedir validação de robô, resolva manualmente no navegador e pressione `Enter` no terminal.

Arquivos de sessão, logs e CSVs de consulta são ignorados pelo git para evitar subir dados pessoais.

## Ajustes úteis

Edite as constantes no começo de `crm_tse_bot.py` se precisar mudar URL, quantidade por execução ou palavras que fazem o robô parar.
