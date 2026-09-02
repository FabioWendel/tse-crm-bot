# Modo normal: somente Pendentes

Abra `INICIAR-SOMENTE-PENDENTES.cmd` na pasta principal. Esta opção usa o mesmo
fluxo de consulta e gravação da versão normal, mas monta a fila diretamente das
páginas da aba **Pendentes**, sem percorrer a base completa da API.

Use este modo sozinho. Enquanto ele percorre as páginas para criar a fotografia
inicial, nenhum outro bot deve retirar pessoas de Pendentes. Se outra máquina ou
operador estiver salvando ao mesmo tempo, a paginação pode pular ou repetir
linhas.

No teto, `0` ou `Enter` significa processar toda a fotografia. Pessoas incluídas
em Pendentes depois da fotografia inicial ficam para a próxima execução.

Este modo compartilha os perfis e relatórios da versão normal. Portanto, não abra
`INICIAR-BRAVE.cmd` e `INICIAR-SOMENTE-PENDENTES.cmd` simultaneamente no mesmo
computador.
