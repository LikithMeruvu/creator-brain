<!-- cbrain:managed - Creator Brain rewrites this file when it updates. To keep your own edits, delete this line. -->
# Rules for AI agents in this vault

This folder is a "Creator Brain": notes made from YouTube creators' videos. You are allowed to read it, search it
and add to it. Follow these rules every time.

## What is in here

- `Me.md` - who the user is: goal, level, injuries, equipment, days per week. Read it before you give advice.
- `Home.md` - the dashboard. Shows each creator and how complete their brain is.
- `creators/<Name>/<Name>.md` - one note per creator, with stats and their top videos.
- `creators/<Name>/videos/` - one note per video. The full transcript, with a timestamp link after every paragraph.
- `creators/<Name>/updates/` - what changed each time we checked for new videos.
- `creators/<Name>/<Name> - All Videos.md` - every upload on the channel. ✅ = in the brain, ➕ = not added yet.
  If the user's question needs a ➕ video, offer to add it with the `add_videos` tool (match words in its title).
- `topics/` - one note per subject (for example "Protein Intake"). Lists what each creator says, newest first.
- `outputs/` - answers and plans saved from earlier chats. Good to re-read, never the final truth.
- `.brain/` - the engine room. Do not read or edit it by hand.

## How to answer a question

1. **Search first.** Use the MCP tool `search_brain` (or run `brain search "your question"` in a terminal).
   Add a creator filter when the user names someone. Search before you answer, even if you think you know.
2. **Read the best hits.** Use `get_topic` for a whole subject, or open the video note for more context.
3. **Cite with a timestamp link.** Every claim you pass on needs a link like
   `[▶ 12:30](https://www.youtube.com/watch?v=ID&t=750s)`. The user must be able to click and hear it.
4. **Prefer new advice.** If two videos disagree, the newer one usually wins. Text in `~~strikethrough~~`
   or marked "changed" is outdated. Mention the change instead of hiding it.
5. **Show both sides when creators disagree.** Say who thinks what, with links. Do not pick silently.
6. **Make it personal.** Read `Me.md`. Fit the advice to the user's goal, level, injuries and equipment.
   For anything about health or injury, add one short line to check with a doctor or physio.
7. **Never invent.** If search finds nothing, say "the brain has no video on this yet." Do not fill the gap
   with a guess and do not attach a creator's name to something they did not say.
   Words in quotation marks must come from the transcript. Your own opinion must be clearly labelled as yours.
8. **Say how complete the brain is.** `search_brain` and `brain_status` report how much is built (for example
   "64% built, 3 videos parked"). Tell the user when the answer may be missing videos.

## Saving your work

When you write something useful (a plan, a comparison, a summary), save it with `save_output`.
Give it a clear title, the Markdown, and a list of sources (the video note names or links you used).
It goes into `outputs/` and links back to its sources, so the graph stays connected.

## Do not

- Edit notes in `creators/` or `topics/` by hand. They are rebuilt from the transcripts and your edits will be lost.
  Put your own notes in `outputs/`.
- Change `Me.md` fields unless the user told you to. Use the `my_profile` tool.
- Copy long passages. Quote a sentence or two and link to the rest.
- Pretend a search result is a transcript you read. Say which one you actually opened.

## Tools you can call

`search_brain`, `get_topic`, `compare_creators`, `my_profile`, `save_output`, `brain_status`,
`add_creator`, `update_creators`, `suggest_creators`, `brain_doctor`.
New videos are checked about once a day. You can ask `update_creators` for a fresh check.
