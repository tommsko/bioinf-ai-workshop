# Shared workshop software environment. Use:  source /storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop/activate.sh
_WS_ROOT=/storage/praha5-elixir/projects/bioinf-fi/tomsko/workshop
if [ -f "$_WS_ROOT/miniforge/etc/profile.d/conda.sh" ]; then
  source "$_WS_ROOT/miniforge/etc/profile.d/conda.sh"
  conda activate "$_WS_ROOT/miniforge/envs/workshop"
else
  echo "workshop env not installed at $_WS_ROOT" >&2
fi
unset _WS_ROOT
