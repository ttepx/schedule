"""Дані викладачів ЦК ЗОД (з PDF «Покликання на конференцію zoom»).

УВАГА: тут коди доступу до Zoom. Тримай репозиторій GitHub ПРИВАТНИМ.
"""

TEACHERS = [
    {"name": "Антоневич Ольга Йосипівна", "short": "Антоневич О. Й.", "url": "https://us05web.zoom.us/j/3132888059?pwd=0wofkLKtYAXgZG7dRN49pRFtjJQtgI.1", "ids": ["3132888059"], "code": "D6cDxQ"},
    {"name": "Антоневич Юрій Адамович", "short": "Антоневич Ю. А.", "url": "https://us05web.zoom.us/j/82930704049?pwd=kwTYNYbaibaUvIAKL2nGiPuBiy1MnL.1", "ids": ["82930704049"], "code": "3DdtGY"},
    {"name": "Баша Олександр Миколайович", "short": "Баша О. М.", "url": "https://us05web.zoom.us/j/7244179931?pwd=bDb97OgCT6rU60QWGV67v1ywlyvGGD.1&omn=85458478768", "ids": ["7244179931"], "code": "6qQHY2"},
    {"name": "Бойко Людмила Анатоліївна", "short": "Бойко Л. А.", "url": "https://us05web.zoom.us/j/9390381517?pwd=RFEyISCAx8gdt1NI0O28eJMzQDTiaI.1", "ids": ["9390381517"], "code": "1111"},
    {"name": "Босюк Андрій Володимирович", "short": "Босюк А. В.", "url": "https://us05web.zoom.us/j/8564315348?pwd=cDhltrInlcegkcUAAt9MDfd5qCx8IP.1", "ids": ["8564315348"], "code": "1111"},
    {"name": "Веремчук Ірина Юріївна", "short": "Веремчук І. Ю.", "url": "https://us05web.zoom.us/j/5142019011?pwd=FUtbbEWZbOECLdx5UlbG91BfGXkx6n.1", "ids": ["5142019011"], "code": "x7rLfN"},
    {"name": "Герасимчук Артем Віталійович", "short": "Герасимчук А. В.", "url": "https://us04web.zoom.us/j/8733100497?pwd=Unlmc2NUa3lSQWpJU3JNMno3bGdvdz09&omn=75398874142", "ids": ["8733100497"], "code": "sN5X4g"},
    {"name": "Грицик Тетяна Андріївна", "short": "Грицик Т. А.", "url": "https://zoom.us/j/9327331772?pwd=aklvMHZoTkl4RCt1dUFVTW9EZjBqdz09", "ids": ["9327331772"], "code": "8dHbzJ"},
    {"name": "Гуменюк Валерій Дмитрович", "short": "Гуменюк В. Д.", "url": "https://us05web.zoom.us/j/2121146846?pwd=H24kK6fVJm6FlrN2zrvSGnVKm3AFtb.1", "ids": ["2121146846"], "code": "1968"},
    {"name": "Дмитрук Андрій Володимирович", "short": "Дмитрук А. В.", "url": "https://us05web.zoom.us/j/2530907523?pwd=SMgTfbHxCiwnPI8T7EjJPYIuXxygHK.1", "ids": ["2530907523"], "code": "12345"},
    {"name": "Зіняч Вікторія Вікторівна", "short": "Зіняч В. В.", "url": "https://us05web.zoom.us/j/6190298540?pwd=ai9rODZqOXhsOUxJRmNENTBtWEJUQT09", "ids": ["6190298540"], "code": "3m0rNi"},
    {"name": "Капітула Ігор Карпович", "short": "Капітула І. К.", "url": "https://us05web.zoom.us/j/5694423779?pwd=O3SK6aiuNVavWA2lJ5KWx8dtlYZVln.1", "ids": ["5694423779"], "code": "12345"},
    {"name": "Насинюк Анастасія Ігорівна", "short": "Насинюк А. І.", "url": "https://us05web.zoom.us/j/5694423779?pwd=O3SK6aiuNVavWA2lJ5KWx8dtlYZVln.1", "ids": ["5694423779"], "code": "12345"},
    {"name": "Остапчук Микола Васильович", "short": "Остапчук М. В.", "url": "https://us04web.zoom.us/j/76770762191?pwd=iQwQIZKSbjHs7W6DVUMf9584JzG6gN.1", "ids": ["76770762191"], "code": "9LJaB1"},
    {"name": "Петрівська Людмила Олексіївна", "short": "Петрівська Л. О.", "url": "https://us04web.zoom.us/j/3259951106?pwd=fubEwW6asXebhiRzPijWtjVFjWFeKe.1", "ids": ["3259951106"], "code": "11111"},
    {"name": "Рафальський Олег Антонович", "short": "Рафальський О. А.", "url": "https://us05web.zoom.us/j/2046235713?pwd=WlVmQUt4QnhIREtEeHNzaW9rV2p6QT09", "ids": ["2046235713"], "code": "1966"},
    {"name": "Свінтозельський Назар Віталійович", "short": "Свінтозельський Н. В.", "url": "https://us05web.zoom.us/j/8364629177?pwd=xmu69f0vFBoIU9Yg3tzSvFdxMYyNiv.1", "ids": ["8364629177"], "code": "5peH9r"},
    {"name": "Тригубець Лариса Романівна", "short": "Тригубець Л. Р.", "url": "https://zoom.us/j/4751881089?pwd=WlZwUU1RZGFaaE1qN2gxazk0SDBsUT09", "ids": ["4751881089"], "code": "1234l"},
    {"name": "Турчик Олег Юрійович", "short": "Турчик О. Ю.", "url": "https://us05web.zoom.us/j/5847488829?pwd=4tNb4b3tAF8vPq5qFTwXBesHpGscol.1", "ids": ["5847488829"], "code": "1111"},
    {"name": "Христянович Володимир Миколайович", "short": "Христянович В. М.", "url": "https://us05web.zoom.us/j/3758095002?pwd=Jk9G5YZVOIb500St0dUqWjIq5UxbiG.1", "ids": ["3758095002"], "code": "1111"},
    {"name": "Шевчук Тетяна Миколаївна", "short": "Шевчук Т. М.", "url": "https://us05web.zoom.us/j/8682399756?pwd=PUmcK3jA9br714jJCovZa2g3vRNNT5.1", "ids": ["8682399756"], "code": "Xct5HF"},
    {"name": "Шелепенко Олексій Ігорович", "short": "Шелепенко О. І.", "url": "https://us05web.zoom.us/j/3385746879?pwd=Mm5abzdPRjRBR0N6MGFKeUN1ZXNFUT09", "ids": ["3385746879"], "code": "1uLMX2"},
    {"name": "Шмігель Вікторія Марківна", "short": "Шмігель В. М.", "url": "https://us04web.zoom.us/launch/jc/74820044197", "ids": ["5248135860", "74820044197"], "code": "4321"},
    {"name": "Юрко Петро Андрійович", "short": "Юрко П. А.", "url": "https://us05web.zoom.us/j/8049481016?pwd=xGVOMiPO69scMEtTnxXzy5PHjw99KE.1", "ids": ["8049481016"], "code": "Xt70PB"},
    {"name": "Юхимчук Юлія Петрівна", "short": "Юхимчук Ю. П.", "url": "https://us04web.zoom.us/j/7954862869?pwd=K0VNWkkvWW1ydjkyeDZhR2prY085Zz09", "ids": ["9080095117", "7954862869"], "code": "y5Wunq"},
]

# Хто який предмет веде. Формат:
#   "група": {"частина назви предмета": "Прізвище І. П."}
# Назва групи: перше слово біля групи в lCloud (наприклад "11-ІП").
# Регістр літер у назві предмета неважливий. Приклад:
#   "11-ІП": {
#       "Інформатика": "Антоневич Ю. А.",
#       "Фізика": "Шевчук Т. М.",
#   },
ASSIGNMENTS = {
}
