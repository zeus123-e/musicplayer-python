# Console Music Player

Player de musica pelo console no Windows que busca audio no YouTube, toca com fila e usa comandos no estilo `m!`.
O console usa `prompt-toolkit` para manter o prompt `>` arrumado mesmo quando a musica comeca a tocar enquanto voce esta digitando.

## Instalar

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Rodar

```powershell
python main.py
```

## Comandos

```text
m!p "nome da musica"  adiciona uma musica do YouTube na fila
m!s                   para a musica atual e pula para a proxima
m!fila                mostra a fila
m!limpar              limpa a fila pendente
m!q                   sai do player
m!help                mostra os comandos
```

Quando voce usa `m!p`, o player procura no YouTube antes de enfileirar. Assim, mesmo se o nome estiver meio errado, ele mostra o titulo encontrado:

```text
Nome certo da musica adicionado a fila (posicao 1)
```

Tambem da para usar uma URL:

```text
m!p https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

## Testes

```powershell
python -m unittest discover -s tests
```

Os testes usam downloader/player falsos, entao nao precisam baixar musica nem tocar audio.
