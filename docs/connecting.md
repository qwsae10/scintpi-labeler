# Connecting to the labeler

The labeler runs on ganymede2 (the UT Dallas HPC cluster). Your laptop only
needs an SSH client and a web browser. Nothing is installed on your laptop and
no data is copied to it. Every label is saved on ganymede the moment you press
a key.

## One-time setup

1. **Get a ganymede account.** Request one from UTD CIRC with your NetID, and
   ask Isaac to add you to the `uars` group. You need that group to read the
   data and save labels. Check that it worked:

   ```bash
   ssh <netid>@ganymede2.utdallas.edu 'id; ls /titan/frodrigues/scint_labeler'
   ```

   `uars` should appear in the `groups=` list.

2. **Off campus?** Connect to the UTD VPN first. ganymede only accepts
   connections from the campus network.

3. **SSH client.**
   - macOS and Linux: use the Terminal app.
   - Windows 10/11: use PowerShell. It includes `ssh`.

4. **Optional: SSH key, so you don't type your password every time.**

   ```bash
   ssh-keygen -t ed25519            # press Enter at every question
   ssh-copy-id <netid>@ganymede2.utdallas.edu    # macOS/Linux
   ```

   On Windows, append the contents of `C:\Users\<you>\.ssh\id_ed25519.pub` to
   `~/.ssh/authorized_keys` on ganymede.

## Every session

1. Open a terminal and run this one command. Replace `<netid>`:

   ```bash
   ssh -t -L 8765:localhost:8765 <netid>@ganymede2.utdallas.edu /titan/frodrigues/scint_labeler/code/cluster/serve.sh 8765
   ```

   This logs in, starts your own copy of the labeler on the login node, and
   connects port 8765 on your laptop to it. Keep the window open. It prints
   `scintlab is running for user <netid>`.

2. Open **http://localhost:8765** in your browser.

3. Label. When you are done, close the browser tab and press **Ctrl-C** in
   the terminal (or just close the terminal). You lose nothing: every decision
   was saved when you made it.

4. Next time, run the same command. The queue starts where you stopped.

### If something goes wrong

| Message | What to do |
|---|---|
| `Port 8765 is already in use` | Someone else is using that port on the login node. Use another number in both places, e.g. `-L 8766:localhost:8766 ... serve.sh 8766`, then open http://localhost:8766. |
| `bind [127.0.0.1]:8765: Address already in use` (on your laptop) | An old tunnel is still open. Close other terminals, or use another number. |
| `You cannot write to .../labels` | You are not in the `uars` group yet. Ask Isaac. |
| Browser says "connection refused" | The terminal window was closed or the VPN dropped. Run the command again. |
| A tab shows "No 20 Hz data stored" | High-rate data is only kept near high-S4 minutes. Use a smaller window (± minutes). |

## Why it runs this way

The data (several hundred receiver-days of 20 Hz products) stays on the
cluster. The SSH tunnel keeps the app private to you: it listens only on the
login node's `localhost`, so nobody else on the cluster can open it or label
as you. Each user writes only their own label file
(`/titan/frodrigues/scint_labeler/labels/<netid>.jsonl`), so several people
can label at the same time.
