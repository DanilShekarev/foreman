# Сторонние компоненты

Фронтенд не тянет ничего с CDN — все сторонние библиотеки вендорятся
в `web/vendor/`. Список и лицензии:

| Библиотека | Версия | Лицензия | Файлы |
|---|---|---|---|
| [CodeMirror 5](https://codemirror.net/5/) | 5.65.x | MIT | `codemirror.min.js`, `codemirror.min.css`, `cm-markdown.min.js`, `cm-xml.min.js`, `cm-continuelist.min.js` |
| [TOAST UI Editor](https://ui.toast.com/tui-editor) | 3.2.2 | MIT | `toastui-editor-all.min.js`, `toastui-editor.min.css`, `toastui-editor-dark.min.css`, `toastui-editor-ru-ru.min.js` |
| [RNNoise](https://github.com/xiph/rnnoise) | сборка [@jitsi/rnnoise-wasm](https://github.com/jitsi/rnnoise-wasm) 0.2.1 | BSD-3-Clause (RNNoise) + Apache-2.0 (сборка) | `rnnoise.wasm` |

У минифицированных сборок CodeMirror лицензионные заголовки вырезаны
минификацией — полный текст лицензии лежит в `web/vendor/LICENSE-CodeMirror.txt`.
Заголовки TOAST UI сохранены в самих файлах. Тексты обеих лицензий RNNoise —
в `web/vendor/LICENSE-rnnoise.txt`; сам бинарник взят из `dist/rnnoise.wasm`
одноимённого npm-пакета без изменений.

Python-зависимости ставятся из PyPI (`requirements.txt`) и в репозиторий
не входят.
