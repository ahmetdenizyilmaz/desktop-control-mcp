# A disposable target for the end-to-end test.
#
# Notepad is a poor fixture for an automated run: closing it with unsaved text
# pops a "Save changes?" prompt so the window never goes away, the packaged
# build opens a File Explorer window on every launch, and a real one may hold
# the user's own unsaved work that a typing test must not touch.
#
# Tk is no good either -- it publishes nothing below the title bar to UI
# Automation, so a typed value cannot be read back to verify it landed.
#
# WinForms exposes a proper UIA EditControl with a Value pattern, and closes
# immediately and unconditionally. It exists only while the test runs.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form            = New-Object System.Windows.Forms.Form
$form.Text       = "E2E TARGET"
$form.Size       = New-Object System.Drawing.Size(560, 240)
$form.StartPosition = "Manual"
$form.Location   = New-Object System.Drawing.Point(120, 120)

$label           = New-Object System.Windows.Forms.Label
$label.Text      = "Type here"
$label.Location  = New-Object System.Drawing.Point(24, 24)
$label.AutoSize  = $true
$form.Controls.Add($label)

$box             = New-Object System.Windows.Forms.TextBox
$box.Name        = "input"
$box.AccessibleName = "Type here"
$box.Location    = New-Object System.Drawing.Point(24, 52)
$box.Size        = New-Object System.Drawing.Size(490, 28)
$form.Controls.Add($box)

$status          = New-Object System.Windows.Forms.Label
$status.Text     = "ready"
$status.Location = New-Object System.Drawing.Point(24, 96)
$status.AutoSize = $true
$form.Controls.Add($status)

$box.Add_TextChanged({ $status.Text = "$($box.Text.Length) characters" })

$form.Add_Shown({ $form.Activate(); $box.Focus() })
[System.Windows.Forms.Application]::Run($form)
