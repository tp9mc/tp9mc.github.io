-- MacScrub GUI — нативный интерфейс на AppleScript (без зависимостей).
-- Компилируется в MacScrub.app и вызывает движок Contents/Resources/bin/macscrub.

property pTitle : "MacScrub — очистка macOS"

on binPath()
	set appPath to POSIX path of (path to me)
	return appPath & "Contents/Resources/bin/macscrub"
end binPath

-- Выполнить движок с аргументами, вернуть stdout+stderr.
on runEngine(args)
	set cmd to quoted form of binPath() & " " & args & " 2>&1"
	try
		return do shell script cmd
	on error errMsg
		return "Ошибка: " & errMsg
	end try
end runEngine

-- Выбор периода очистки.
on chooseWindow()
	set opts to {"Этот день", "Эта неделя", "Этот месяц", "Всё время"}
	set sel to choose from list opts with prompt "За какой период чистить следы?" default items {"Этот день"} without multiple selections allowed
	if sel is false then error number -128
	set s to item 1 of sel
	if s is "Этот день" then
		return "this-day"
	else if s is "Эта неделя" then
		return "this-week"
	else if s is "Этот месяц" then
		return "this-month"
	else
		return "all"
	end if
end chooseWindow

-- Выбор категорий. Возвращает строку id через запятую или "" (значит набор по умолчанию).
on chooseCategories()
	set raw to runEngine("categories")
	set catLines to paragraphs of raw
	set displayList to {}
	set idList to {}
	repeat with ln in catLines
		set lns to (ln as text)
		if lns is not "" and lns does not start with "ID" and lns does not start with "──" then
			-- формат: id  риск  sudo  Описание
			set AppleScript's text item delimiters to " "
			set firstWord to text item 1 of lns
			set AppleScript's text item delimiters to ""
			if firstWord is not "" then
				set end of idList to firstWord
				set end of displayList to lns
			end if
		end if
	end repeat
	set sel to choose from list displayList with prompt "Выберите категории очистки (Cmd для нескольких). Отмена = безопасный набор по умолчанию." with multiple selections allowed
	if sel is false then return ""
	set ids to {}
	repeat with chosen in sel
		set chosenText to (chosen as text)
		set AppleScript's text item delimiters to " "
		set cid to text item 1 of chosenText
		set AppleScript's text item delimiters to ""
		set end of ids to cid
	end repeat
	set AppleScript's text item delimiters to ","
	set res to ids as text
	set AppleScript's text item delimiters to ""
	return res
end chooseCategories

on openLatestReport()
	set repDir to paragraph -1 of runEngine("report dir")
	try
		-- открыть самый свежий .txt-отчёт в TextEdit; иначе показать папку
		do shell script "f=$(ls -t " & quoted form of repDir & "/report_*.txt 2>/dev/null | head -1); " & ¬
			"if [ -n \"$f\" ]; then open -e \"$f\"; else open " & quoted form of repDir & "; fi"
	end try
end openLatestReport

-- Анализ (dry-run)
on doScan()
	set win to chooseWindow()
	set cats to chooseCategories()
	set extra to " --window " & win
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("scan" & extra)
	set summary to my tailLines(out, 6)
	display dialog "АНАЛИЗ завершён (ничего не удалено)." & return & return & summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doScan

-- Очистка (реальное удаление)
on doClean()
	set win to chooseWindow()
	set cats to chooseCategories()
	set methodSel to choose from list {"В Корзину (безопасно)", "Удалить безвозвратно"} with prompt "Способ удаления:" default items {"В Корзину (безопасно)"} without multiple selections allowed
	if methodSel is false then error number -128
	set delFlag to ""
	if (item 1 of methodSel) is "Удалить безвозвратно" then set delFlag to " --delete"

	set confirm to display dialog "Будет выполнено РЕАЛЬНОЕ удаление." & return & "Период: " & win & return & "Отмена возможна только сейчас." buttons {"Отмена", "Очистить"} default button "Отмена" with icon caution with title pTitle
	if button returned of confirm is "Отмена" then return

	set extra to " --apply --yes --window " & win & delFlag
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("clean" & extra)
	set summary to my tailLines(out, 6)
	display dialog "ОЧИСТКА завершена." & return & return & summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doClean

-- Управление отчётами
on doReports()
	set out to runEngine("report list")
	set act to display dialog "Отчёты:" & return & return & out buttons {"Удалить все", "Открыть папку", "Закрыть"} default button "Закрыть" with title pTitle
	if button returned of act is "Удалить все" then
		set c to display dialog "Удалить ВСЕ отчёты?" buttons {"Отмена", "Удалить"} default button "Отмена" with icon caution
		if button returned of c is "Удалить" then
			runEngine("report clear")
			display dialog "Отчёты удалены." buttons {"ОК"} default button "ОК" with title pTitle
		end if
	else if button returned of act is "Открыть папку" then
		set rep to runEngine("report dir")
		do shell script "open " & quoted form of (paragraph -1 of rep)
	end if
end doReports

-- Расписание
on doSchedule()
	set st to runEngine("schedule status")
	set act to choose from list {"Установить ежедневно", "Установить еженедельно", "Удалить расписание", "Статус"} with prompt ("Текущий статус:" & return & st) without multiple selections allowed
	if act is false then return
	set a to item 1 of act
	if a is "Статус" then
		display dialog st buttons {"ОК"} default button "ОК" with title pTitle
		return
	else if a is "Удалить расписание" then
		set out to runEngine("schedule uninstall")
		display dialog out buttons {"ОК"} default button "ОК" with title pTitle
		return
	end if

	set tm to text returned of (display dialog "Время запуска (ЧЧ:ММ):" default answer "03:00" with title pTitle)
	set win to chooseWindow()
	set freq to "daily"
	set extraDay to ""
	if a is "Установить еженедельно" then
		set freq to "weekly"
		set wd to text returned of (display dialog "День недели (1=Пн … 7=Вс):" default answer "1" with title pTitle)
		set extraDay to " --weekday " & wd
	end if
	set out to runEngine("schedule install --freq " & freq & " --at " & tm & " --window " & win & extraDay)
	display dialog out buttons {"ОК"} default button "ОК" with title pTitle
end doSchedule

-- Рекомендации (локальный анализ, без сети)
on doAdvise()
	set win to chooseWindow()
	set cats to chooseCategories()
	set extra to " --window " & win
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("advise" & extra)
	-- показываем блок рекомендаций (хвост вывода)
	set summary to my tailLines(out, 18)
	display dialog summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doAdvise

on tailLines(txt, n)
	set ps to paragraphs of txt
	set c to count of ps
	if c ≤ n then return txt
	set startI to c - n + 1
	set AppleScript's text item delimiters to return
	set res to (items startI thru c of ps) as text
	set AppleScript's text item delimiters to ""
	return res
end tailLines

on run
	repeat
		set choice to choose from list ¬
			{"Анализ (посмотреть, сколько мусора)", "Очистка (удалить)", "Рекомендации", "Отчёты", "Расписание", "Выход"} ¬
			with prompt "MacScrub — что делаем?" with title pTitle without multiple selections allowed
		if choice is false then exit repeat
		set c to item 1 of choice
		try
			if c starts with "Анализ" then
				doScan()
			else if c starts with "Очистка" then
				doClean()
			else if c is "Рекомендации" then
				doAdvise()
			else if c is "Отчёты" then
				doReports()
			else if c is "Расписание" then
				doSchedule()
			else
				exit repeat
			end if
		on error errMsg number errNum
			if errNum is not -128 then display dialog "Ошибка: " & errMsg buttons {"ОК"} default button "ОК"
		end try
	end repeat
end run
