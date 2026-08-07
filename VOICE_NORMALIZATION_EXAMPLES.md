# Voice normalization examples

Vocast now rewrites unambiguous English notation into words before text is
chunked and sent to either Kokoro engine. The stored article text is unchanged;
these rewrites affect only newly synthesized audio.

## Numbers

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Cardinal numbers | `The network serves 100000 listeners.` | `The network serves one hundred thousand listeners.` |
| Grouped numbers | `The shipment contains 1,234 units.` | `The shipment contains one thousand two hundred thirty-four units.` |
| Natural hundreds | `The room holds 1,200 people.` | `The room holds twelve hundred people.` |
| Grouping spaces | `The room holds 1 200 people.` (with a narrow nonbreaking space) | `The room holds twelve hundred people.` |
| Decimals | `Version 5.6 is ready.` | `Version five point six is ready.` |
| Decimal precision | `The result was 1.230.` | `The result was one point two three zero.` |
| Leading zeroes | `Agent 007 arrived.` | `Agent zero zero seven arrived.` |
| Signed numbers | `The change was -12.` | `The change was minus twelve.` |
| Percentages | `Accuracy reached 99.5%.` | `Accuracy reached ninety-nine point five percent.` |
| Ordinals | `She finished 21st.` | `She finished twenty-first.` |
| Grouped ordinals | `It was the 1,000th run.` | `It was the one thousandth run.` |
| En/em-dash ranges | `The range was 5–10%.` | `The range was five to ten percent.` |
| Dash-separated values | `The score was 3–1.` | `The score was three to one.` |
| Decades | `Culture changed in the 1960s.` | `Culture changed in the nineteen sixties.` |
| Month dates | `On July 21, 2026, it changed.` | `On July twenty-first, two thousand twenty-six, it changed.` |
| Abbreviated year ranges | `The 2014-15 season changed.` | `The twenty fourteen to twenty fifteen season changed.` |
| Unambiguous slash dates | `Reading List 07/25/26.` | `Reading List July twenty-fifth, twenty twenty-six.` |

Four-digit multiples from 1100 through 1900 use the familiar "twelve
hundred" form. Other values use ordinary American cardinals.

## Currency

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Currency after amount | `The budget was $100000.` | `The budget was one hundred thousand dollars.` |
| Singular units | `It cost $1.` | `It cost one dollar.` |
| Dollars and cents | `It cost $12.50.` | `It cost twelve dollars and fifty cents.` |
| Cents only | `It cost $0.01.` | `It cost one cent.` |
| Pounds and pence | `The fee was £2.01.` | `The fee was two pounds and one penny.` |
| Euros and cents | `The fee was €1.50.` | `The fee was one euro and fifty cents.` |
| Compact magnitudes | `Revenue reached $1.2bn.` | `Revenue reached one point two billion dollars.` |
| Currency ranges | `The estimate was $5–$10.` | `The estimate was five dollars to ten dollars.` |
| Signed currency | `The balance was -$12.` | `The balance was minus twelve dollars.` |

Dollar, pound, and euro symbols are supported. `k`, `m`, `bn`, `million`,
`billion`, and `trillion` are supported when attached to a currency amount.

## Roman Numerals

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Numbered sections | `Chapter III explains it.` | `Chapter three explains it.` |
| Historical names | `Henry VIII ruled.` | `Henry the eighth ruled.` |
| Named events | `World War II ended.` | `World War two ended.` |
| Single contextual numeral | `See Appendix V.` | `See Appendix five.` |
| Larger contextual numeral | `Super Bowl LVIII drew viewers.` | `Super Bowl fifty-eight drew viewers.` |
| Standalone section marker | `III` | `three` |

Roman conversion requires section/event context, a preceding proper name, or a
standalone numeral. Ambiguous initialisms remain initialisms: `Use I.V. access`
becomes `Use I V access`, not `Use four access`. The pronoun `I` is always
preserved.

## Dots And Abbreviations

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Dotted initialism | `U.S. policy changed.` | `US policy changed.` |
| Several initialisms | `The U.K. and E.U. agreed.` | `The UK and EU agreed.` |
| Personal initials | `J. R. R. Tolkien wrote it.` | `JRR Tolkien wrote it.` |
| Example abbreviation | `Use fruit, e.g. apples.` | `Use fruit, for example, apples.` |
| Explanation abbreviation | `It is red, i.e. warm.` | `It is red, that is, warm.` |
| Other common abbreviations | `Apples, etc. work vs. pears.` | `Apples, et cetera work versus pears.` |
| Titles before names | `Dr. Smith and Prof. Lee spoke.` | `Doctor Smith and Professor Lee spoke.` |
| Sentence-final initialism | `He said "U.S." Then left.` | `He said "US." Then left.` |

Internal dots are removed or expanded without discarding a period that also
ends a sentence.

## Sections And Unicode

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Unpunctuated section | `Overview` + blank line + `The details follow.` | `Overview.` + blank line + `The details follow.` |
| Short heading line | `Market Outlook` + newline + `Prices rose.` | `Market Outlook.` + newline + `Prices rose.` |
| Windows line endings | `Overview` + CRLF/blank line + `Details.` | `Overview.` + LF/blank line + `Details.` |
| Unicode minus | `The change was -12.` (with a Unicode minus) | `The change was minus twelve.` |
| Fullwidth punctuation | `Ready! Next?` (with fullwidth marks) | `Ready! Next?` (with ASCII marks) |
| Invisible web characters | `word` + soft hyphen + `break` | `wordbreak` |
| Fullwidth ampersand | `Goodbye ＆ more.` | `Goodbye & more.` |

A blank line creates a sentence stop when the preceding section has none. A
single newline does so only for a short title-cased heading; lowercase soft
wraps are preserved.

## Measurements And Symbols

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Temperature | `It is 20°C outside.` | `It is twenty degrees Celsius outside.` |
| Speed | `Speed was 60 mph.` | `Speed was sixty miles per hour.` |
| Medical units | `Take 400 IU daily.` | `Take four hundred international units daily.` |
| Data sizes | `The file is 4GB.` | `The file is four gigabytes.` |
| Frequency | `The rate is 10 Hz.` | `The rate is ten hertz.` |
| Metric units | `Use 1 mg in 2 mL.` | `Use one milligram in two milliliters.` |
| Compound units | `It moved at 5 m/s.` | `It moved at five meters per second.` |
| Approximation | `Use ~25 nmol/L.` | `Use approximately twenty-five nanomoles per liter.` |
| Comparisons | `Use <5 and >10 samples.` | `Use less than five and greater than ten samples.` |
| Issue numbers | `Issue #41 and PR #287 landed.` | `Issue number forty-one and PR number two hundred eighty-seven landed.` |
| AI initialisms | `AGI and AIXI differ.` | `Aye Gee Eye and Aye Eye Ex Eye differ.` |
| ASCII arrows | `Values move 0 -> 1.` | `Values move zero right arrow one.` |
| Contextual fractions | `About 2/3 of people agreed.` | `About two thirds of people agreed.` |
| Scientific E notation | `The rate was 1.2e-5.` | `The rate was one point two times ten to the power of minus five.` |
| Powers | `There are 10^24 cases.` | `There are ten to the power of twenty-four cases.` |
| Scientific powers | `The constant is 6.02×10^23.` | `The constant is six point zero two times ten to the power of twenty-three.` |

Units are expanded only when attached to a number and found in the
case-sensitive unit table. Ambiguous standalone letters such as `m`, `M`, `K`,
and `B` are not treated as measurements or magnitudes.

## Corpus Artifacts

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Glued terminal citation | `Done.[4]Eventually it worked.` | `Done. Eventually it worked.` |
| Glued inline citation | `The protein[3]and receptor bind.` | `The protein[three] and receptor bind.` |
| Superscript citation | `That was established.”¹ Next result.` | `That was established.” Next result.` |
| Markdown heading | `## Core Objective` | `Core Objective.` |
| Inline and fenced code | ``Use the `bash` tool.`` | Preserved exactly so code numbers are not rewritten. |
| Footnote backlink | `That was the result. ↩︎` | `That was the result.` |
| Accessibility label | `Details(opens in a new window).` | `Details.` |
| Trailing comma section | `Therefore,` + newline + `Next line.` | Preserved without becoming `Therefore,.` |

Citation removal is deliberately limited to markers attached to terminal
punctuation with no spacing before the following word. Mid-sentence bracket
markers are spaced so words no longer fuse, but remain audible because plain
text cannot reliably distinguish a citation from meaningful indexing.

## Protected Complete Tokens

| Fix | Before | Text sent to the voice |
| --- | --- | --- |
| Product version | `GPT-5.6 Sol shipped.` | `GPT five point six Sol shipped.` |
| Nonbreaking product hyphen | `GPT‑5.6 Sol shipped.` | `GPT five point six Sol shipped.` |
| Full timestamp | `[0:52:25] Wrong model named.` | `[0:52:25] Wrong model named.` |
| ISO timestamp | `2026-08-07T12:30:45Z` | `2026-08-07T12:30:45Z` |

Product versions are recognized as complete tokens before their numeric
components are spoken. Timestamps are preserved as complete tokens. Previously
only `GPT-5` or `0:52` was protected, so the remainder was independently
rewritten into forms such as `GPT-5zero point six` and `0:52:twenty-five`.

## Sentence Boundaries

Chunking now recognizes the sentence stop in all of these forms:

| Before | Chunk boundary |
| --- | --- |
| `He said “Done.” Next begins.` | After the closing quote in `“Done.”` |
| `That is explained.) Next begins.` | After the closing parenthesis |
| `First thought ended… Next begins.` | After the Unicode ellipsis |

This prevents a size-driven hard wrap from overriding a real sentence boundary.
Paragraphs are still grouped into operational chunks, so distinct paragraph
pause lengths remain future architecture work.

## Deliberately Preserved

These forms are ambiguous or have their own structure, so automatic cardinal
rewrites leave them unchanged:

| Form | Example kept unchanged | Reason |
| --- | --- | --- |
| URLs and email | `https://example.com/v1.2?q=1,200` | Punctuation identifies the address. |
| IP addresses | `192.168.1.1` | Dots separate octets, not decimals. |
| Uncued multi-part notation | `0.12.0` | Components may be versions, sections, or identifiers. |
| ISO dates and times | `2026-08-07 at 12:30` | Calendar and clock grammar need separate policies. |
| Phone numbers | `555-1234` and `(212) 555-0199` | They should not become cardinal quantities. |
| Technical identifiers | `SHA-256`, `ISO-8601`, and `ISBN 978-1-4028-9462-6` | Digits identify a standard or record. |
| Invalid grouping | `12,34.56` | The intended locale or value is unclear. |
| Medical initialism | `An IV drip is ready.` | `IV` means intravenous, not Roman four. |
| Roman-looking words | `I agree that MIX is a word.` | Ordinary words and the pronoun `I` are not numerals. |
| Oversized digit strings | Very long record identifiers | They are preserved instead of risking a failed synthesis. |

Bare ASCII hyphen ranges (`5-10`) and uncued slash fractions/dates (`1/2`) are
preserved rather than guessed. Ranges followed by percentages, currencies, or
known units are expanded. Arbitrary acronym expansion, dense mathematics beyond
the documented power forms, and locale-dependent currency codes are also left
for future context-aware handling; ordinary numbers outside protected forms are
still cardinalized.
