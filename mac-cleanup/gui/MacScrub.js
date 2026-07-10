'use strict';
// MacScrub GUI — нативный интерфейс на JXA (JavaScript for Automation).
// Компилируется: osacompile -l JavaScript -o MacScrub.app MacScrub.js
// Вызывает движок Contents/Resources/bin/macscrub. Работает офлайн.

ObjC.import('Foundation');

function run() {
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;

  var TITLE = 'MacScrub — очистка macOS';
  var bundlePath = ObjC.unwrap($.NSBundle.mainBundle.bundlePath);
  var BIN = bundlePath + '/Contents/Resources/bin/macscrub';

  function q(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

  function sh(cmd) {
    try { return app.doShellScript(cmd); }
    catch (e) { return 'Ошибка: ' + (e.message || e); }
  }

  function engine(args) { return sh(q(BIN) + ' ' + args + ' 2>&1'); }

  function tail(text, n) {
    var lines = String(text).split('\n');
    if (lines.length <= n) return text;
    return lines.slice(lines.length - n).join('\n');
  }

  function info(text, buttons) {
    buttons = buttons || ['OK'];
    return app.displayDialog(text, {
      buttons: buttons,
      defaultButton: buttons[buttons.length - 1],
      withTitle: TITLE
    });
  }

  // Выбор периода. Бросает при отмене.
  function chooseWindow() {
    var map = {
      'Этот день': 'this-day',
      'Эта неделя': 'this-week',
      'Этот месяц': 'this-month',
      'Всё время': 'all'
    };
    var sel = app.chooseFromList(Object.keys(map), {
      withPrompt: 'За какой период чистить следы?',
      defaultItems: ['Этот день'],
      multipleSelectionsAllowed: false
    });
    if (sel === false) throw new Error('cancel');
    return map[sel[0]];
  }

  // Выбор категорий. Возвращает строку id через запятую или '' (набор по умолчанию).
  function chooseCategories() {
    var raw = engine('categories --json');
    var cats;
    try { cats = JSON.parse(raw); } catch (e) { return ''; }
    var display = [];
    var byLabel = {};
    for (var i = 0; i < cats.length; i++) {
      var c = cats[i];
      var label = c.name + '  [' + c.risk + (c.sudo ? ', sudo' : '') + ']';
      display.push(label);
      byLabel[label] = c.id;
    }
    var sel = app.chooseFromList(display, {
      withPrompt: 'Выберите категории (Cmd — несколько). Отмена = безопасный набор по умолчанию.',
      multipleSelectionsAllowed: true
    });
    if (sel === false) return '';
    var ids = [];
    for (var j = 0; j < sel.length; j++) {
      if (byLabel[sel[j]]) ids.push(byLabel[sel[j]]);
    }
    return ids.join(',');
  }

  function openLatestReport() {
    var dir = tail(engine('report dir'), 1).trim();
    sh('f=$(ls -t ' + q(dir) + '/report_*.txt 2>/dev/null | head -1); ' +
       'if [ -n "$f" ]; then open -e "$f"; else open ' + q(dir) + '; fi');
  }

  function doScan() {
    var win = chooseWindow();
    var cats = chooseCategories();
    var extra = ' --window ' + win + (cats ? ' --categories ' + q(cats) : '');
    var out = engine('scan' + extra);
    var r = info('АНАЛИЗ завершён (ничего не удалено).\n\n' + tail(out, 6),
                 ['Открыть отчёт', 'OK']);
    if (r.buttonReturned === 'Открыть отчёт') openLatestReport();
  }

  function doClean() {
    var win = chooseWindow();
    var cats = chooseCategories();
    var methodSel = app.chooseFromList(['В Корзину (безопасно)', 'Удалить безвозвратно'], {
      withPrompt: 'Способ удаления:',
      defaultItems: ['В Корзину (безопасно)'],
      multipleSelectionsAllowed: false
    });
    if (methodSel === false) return;
    var delFlag = (methodSel[0] === 'Удалить безвозвратно') ? ' --delete' : '';

    try {
      app.displayDialog('Будет выполнено РЕАЛЬНОЕ удаление.\nПериод: ' + win +
                        '\nОтмена возможна только сейчас.',
                        { buttons: ['Отмена', 'Очистить'], defaultButton: 'Отмена',
                          withIcon: 'caution', withTitle: TITLE });
    } catch (e) { return; } // нажали Отмена

    var extra = ' --apply --yes --window ' + win + delFlag + (cats ? ' --categories ' + q(cats) : '');
    var out = engine('clean' + extra);
    var r = info('ОЧИСТКА завершена.\n\n' + tail(out, 6), ['Открыть отчёт', 'OK']);
    if (r.buttonReturned === 'Открыть отчёт') openLatestReport();
  }

  function doAdvise() {
    var win = chooseWindow();
    var cats = chooseCategories();
    var extra = ' --window ' + win + (cats ? ' --categories ' + q(cats) : '');
    var out = engine('advise' + extra);
    var r = info(tail(out, 20), ['Открыть отчёт', 'OK']);
    if (r.buttonReturned === 'Открыть отчёт') openLatestReport();
  }

  function doReports() {
    var out = engine('report list');
    var r = info('Отчёты:\n\n' + out, ['Удалить все', 'Открыть папку', 'Закрыть']);
    if (r.buttonReturned === 'Удалить все') {
      try {
        app.displayDialog('Удалить ВСЕ отчёты?', {
          buttons: ['Отмена', 'Удалить'], defaultButton: 'Отмена',
          withIcon: 'caution', withTitle: TITLE
        });
        engine('report clear');
        info('Отчёты удалены.');
      } catch (e) { /* отмена */ }
    } else if (r.buttonReturned === 'Открыть папку') {
      var dir = tail(engine('report dir'), 1).trim();
      sh('open ' + q(dir));
    }
  }

  function doSchedule() {
    var st = engine('schedule status');
    var actSel = app.chooseFromList(
      ['Установить ежедневно', 'Установить еженедельно', 'Удалить расписание', 'Статус'],
      { withPrompt: 'Текущий статус:\n' + st, multipleSelectionsAllowed: false });
    if (actSel === false) return;
    var a = actSel[0];

    if (a === 'Статус') { info(st); return; }
    if (a === 'Удалить расписание') { info(engine('schedule uninstall')); return; }

    var tmR = app.displayDialog('Время запуска (ЧЧ:ММ):', { defaultAnswer: '03:00', withTitle: TITLE });
    var tm = tmR.textReturned;
    var win = chooseWindow();
    var freq = 'daily';
    var extraDay = '';
    if (a === 'Установить еженедельно') {
      freq = 'weekly';
      var wdR = app.displayDialog('День недели (1=Пн … 7=Вс):', { defaultAnswer: '1', withTitle: TITLE });
      extraDay = ' --weekday ' + wdR.textReturned;
    }
    var out = engine('schedule install --freq ' + freq + ' --at ' + tm + ' --window ' + win + extraDay);
    info(out);
  }

  // Главное меню
  var menu = {
    'Анализ (посмотреть, сколько мусора)': doScan,
    'Очистка (удалить)': doClean,
    'Рекомендации': doAdvise,
    'Отчёты': doReports,
    'Расписание': doSchedule,
    'Выход': null
  };

  while (true) {
    var choice = app.chooseFromList(Object.keys(menu), {
      withPrompt: 'MacScrub — что делаем?',
      multipleSelectionsAllowed: false
    });
    if (choice === false) break;
    var fn = menu[choice[0]];
    if (!fn) break;
    try { fn(); }
    catch (e) {
      if (String(e.message || e).indexOf('cancel') === -1) {
        try { info('Ошибка: ' + (e.message || e)); } catch (e2) { /* игнор */ }
      }
    }
  }
}
