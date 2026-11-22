# Как опубликовать демомагазин на GitHub Pages

В папке `samokat-demo` уже подготовлен Git-репозиторий с веткой `main`. В `docs` лежит готовый сайт с каталогом, фотографиями, моделью и средой Python. Для публикации достаточно GitHub Desktop и аккаунта GitHub.

## 1. Отправьте проект на GitHub

1. Откройте GitHub Desktop и войдите в свой аккаунт.
2. Выберите **File → Add Local Repository… → Choose…**. Укажите всю папку **samokat-demo**, затем нажмите **Add Repository**.
3. Нажмите **Publish repository** и оставьте имя `samokat-demo`. На бесплатном тарифе GitHub снимите галочку **Keep this code private**, чтобы репозиторий стал публичным. В него войдут исходный код, веса модели и изображения. Нажмите **Publish Repository**.
4. Выберите **Repository → View on GitHub**. На вкладке **Code** должна быть видна папка `docs`.

[Как добавить локальную папку в GitHub Desktop](https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-a-repository-from-your-local-computer-to-github-desktop)

## 2. Включите Pages

1. В репозитории на сайте GitHub откройте **Settings → Pages**.
2. В разделе **Build and deployment** выберите **Source → Deploy from a branch**.
3. Выберите ветку **main** и папку **/docs**. Нажмите **Save**.
4. На вкладке **Actions** дождитесь зелёной отметки у **pages build and deployment**. Вернитесь в **Settings → Pages** и нажмите **Visit site**. Эту ссылку можно открыть на телефоне и отправить участникам демонстрации.

Для репозитория с именем `samokat-demo` адрес имеет вид `https://ВАШ-ЛОГИН.github.io/samokat-demo/`. Если GitHub показывает 404, проверьте статус публикации, источник `main /docs` и наличие `docs/index.html` в репозитории. [Настройка источника публикации в GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)

## 3. Проверьте публичную ссылку

Откройте главную и дождитесь загрузки Python при первом посещении. Нажмите **Обновить** рядом с наборами, добавьте набор в корзину и оформите демозаказ. Откройте профиль, перезагрузите страницу и проверьте, что заказ остался в истории.

**История на устройстве.** Заказы, корзины и события всех трёх демопрофилей сохраняются в IndexedDB конкретного браузера. Очистка данных сайта удаляет эту историю. Для анализа демонстраций каждый участник может выгрузить свои события из **Профиль → Скачать журнал эксперимента**.

## Как обновить опубликованный сайт

Редактируйте файлы в этой папке. После изменения Python-кода, модели или исходных данных выполните из корня репозитория `python3 build_pages.py`, чтобы обновить `docs/shop-engine.zip`. Для изменений HTML, стилей, JavaScript и фотографий в `docs` пересборка не требуется. В GitHub Desktop нажмите **Commit to main**, затем **Push origin**. GitHub Pages обновит сайт по прежнему адресу.

Для предварительного просмотра статической версии выполните в этой папке `python3 -m http.server 8080 --bind 127.0.0.1 --directory docs` и откройте [локальный сайт](http://127.0.0.1:8080/).
