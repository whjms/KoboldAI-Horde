pkg update
termux-setup-storage
yes | pkg install python openssh git
cd ~
git clone https://github.com/db0/KoboldAI-Horde.git
cd KoboldAI-Horde
pip install -r bridge_requirements.txt --user
cp clientData_template.py clientData.py 

if [[ "$1" ]]; then
    echo "" >> clientData.py
    echo "kai_url = $1" >> clientData.py
else
    echo 'No Colab URL provided. Please manually edit your clientData.py and add your colab url in the 'kai_url' variable.'
fi
