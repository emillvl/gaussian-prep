"""Lexical safety for the math source shared with a native TeX compiler."""
import re


# Frozen, reviewed glyph names supported by Matplotlib's TeX symbol table.
# Do not derive this security policy from parser output: text bodies are literal
# to Mathtext, but their original commands execute when copied into native TeX.
_SYMBOLS = frozenset("""
AA AE BbbC BbbN BbbP BbbQ BbbR BbbZ Bumpeq Cap Colon Cup DH Delta Doteq
Downarrow Equiv Finv Game Gamma H Im Join L Lambda Ldsh Leftarrow
Leftrightarrow Lleftarrow Longleftarrow Longleftrightarrow Longrightarrow Lsh
Nearrow Nwarrow O OE Omega P Phi Pi Psi QED Rdsh Re Rightarrow Rrightarrow
Rsh S Searrow Sigma Subset Supset Swarrow Theta Thorn Uparrow Updownarrow
Upsilon Vdash Vert Vvdash Xi aa ac acute acwopencirclearrow adots ae aleph
alpha amalg angle approx approxeq approxident arceq ast asterisk asymp
backcong backepsilon backprime backsim backsimeq backslash bagmember bar
barleftarrow barvee barwedge because beta beth between bigcap bigcirc bigcup
bigodot bigoplus bigotimes bigsqcup bigstar bigtriangledown bigtriangleup
biguplus bigvee bigwedge blacksquare blacktriangle blacktriangledown
blacktriangleleft blacktriangleright bot bowtie boxbar boxdot boxminus boxplus
boxtimes breve bullet bumpeq c candra cap carriagereturn cdot cdotp cdots cent
check checkmark chi circ circeq circlearrowleft circlearrowright circledR
circledS circledast circledcirc circleddash circumflexaccent clubsuit
clubsuitopen colon coloneq combiningacuteaccent combiningbreve
combiningdiaeresis combiningdotabove combiningfourdotsabove
combininggraveaccent combiningoverline combiningrightarrowabove
combiningthreedotsabove combiningtilde complement cong coprod copyright cup
cupdot cupleftarrow curlyeqprec curlyeqsucc curlyvee curlywedge curvearrowleft
curvearrowright cwopencirclearrow d dag dagger daleth danger dashleftarrow
dashrightarrow dashv ddag ddagger ddddot dddot ddot ddots degree delta dh
diamond diamondsuit digamma disin div divideontimes dot doteq doteqdot
dotminus dotplus dots dotsminusdots doublebarwedge downarrow downdownarrows
downharpoonleft downharpoonright downzigzagarrow ell emdash emptyset endash
epsilon eqcirc eqcolon eqdef eqgtr eqless eqsim eqslantgtr eqslantless equal
equalparallel equiv eta eth exists fallingdotseq flat forall frakC frakZ
frown gamma geq geqq geqslant gg ggg gimel gnapprox gneqq gnsim grave greater
gtrapprox gtrdot gtreqless gtreqqless gtrless gtrsim guillemotleft
guillemotright guilsinglleft guilsinglright hat hbar heartsuit hermitmatrix
hookleftarrow hookrightarrow hslash i iiiint iiint iint imageof imath in
increment infty int intercal invnot iota isinE isindot isinobar isins isinvb
jmath k kappa kernelcontraction l lambda lambdabar langle lasp lbrace lbrack
lceil ldots leadsto leftarrow leftarrowtail leftbrace leftharpoonaccent
leftharpoondown leftharpoonup leftleftarrows leftparen leftrightarrow
leftrightarrows leftrightharpoons leftrightsquigarrow leftsquigarrow
leftthreetimes leq leqq leqslant less lessapprox lessdot lesseqgtr lesseqqgtr
lessgtr lesssim lfloor lgroup lhd ll llcorner lll lnapprox lneqq lnsim
longleftarrow longleftrightarrow longmapsto longrightarrow looparrowleft
looparrowright lq lrcorner ltimes macron maltese mapsdown mapsfrom mapsto
mapsup measeq measuredangle measuredrightangle merge mho mid minus minuscolon
models mp mu multimap nLeftarrow nLeftrightarrow nRightarrow nVDash nVdash
nabla napprox natural ncong ne nearrow neg neq nequiv nexists ngeq ngtr
ngtrless ngtrsim ni niobar nis nisd nleftarrow nleftrightarrow nleq nless
nlessgtr nlesssim nmid not notin notsmallowns nparallel nprec npreccurlyeq
nrightarrow nsim nsimeq nsqsubseteq nsqsupseteq nsubset nsubseteq nsucc
nsucccurlyeq nsupset nsupseteq ntriangleleft ntrianglelefteq ntriangleright
ntrianglerighteq nu nvDash nvdash nwarrow o obar ocirc odot oe oequal oiiint
oiint oint omega ominus oplus origof oslash otimes overarc overleftarrow
overleftrightarrow parallel partial perp perthousand phi pi pitchfork plus
pm prec precapprox preccurlyeq preceq precnapprox precnsim precsim prime prod
propto prurel psi quad questeq rangle rasp ratio rbrace rbrack rceil rfloor
rgroup rhd rho rightModels rightangle rightarrow rightarrowbar rightarrowtail
rightassert rightbrace rightharpoonaccent rightharpoondown rightharpoonup
rightleftarrows rightleftharpoons rightparen rightrightarrows rightsquigarrow
rightthreetimes rightzigzagarrow ring risingdotseq rq rtimes scrB scrE scrF
scrH scrI scrL scrM scrR scre scrg scro scurel searrow setminus sharp sigma
sim simeq simneqq sinewave slash smallin smallintclockwise smallointctrcclockwise
smallowns smallsetminus smallvarointclockwise smile solbar spadesuit
spadesuitopen sphericalangle sqcap sqcup sqsubset sqsubseteq sqsubsetneq
sqsupset sqsupseteq sqsupsetneq ss star stareq sterling subset subseteq
subseteqq subsetneq subsetneqq succ succapprox succcurlyeq succeq succnapprox
succnsim succsim sum supset supseteq supseteqq supsetneq supsetneqq swarrow t
tau textasciiacute textasciicircum textasciigrave textasciitilde textexclamdown
textquestiondown textquotedblleft textquotedblright therefore theta thickspace
thorn tilde times to top triangle triangledown triangleeq triangleleft
trianglelefteq triangleq triangleright trianglerighteq turnednot twoheaddownarrow
twoheadleftarrow twoheadrightarrow twoheaduparrow ulcorner underbar unlhd unrhd
uparrow updownarrow updownarrowbar updownarrows upharpoonleft upharpoonright
uplus upsilon upuparrows urcorner vDash varepsilon varisinobar varisins varkappa
varlrtriangle varniobar varnis varnothing varphi varpi varpropto varrho
varsigma vartheta vartriangle vartriangleleft vartriangleright vdash vdots
vec vee veebar veeeq vert wedge wedgeq widebar widehat widetilde wp wr xi yen zeta
""".split())

_PRESENTATION = frozenset("""
frac dfrac binom genfrac sqrt text operatorname boldsymbol phantom llap rlap
overline underline overset underset substack left right middle hspace
enspace qquad thinspace mathring overrightarrow
rm cal it tt sf bf bfit default bb frak scr regular normal
mathrm mathcal mathit mathtt mathsf mathbf mathbfit mathdefault mathbb
mathfrak mathscr mathregular mathnormal
Pr arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom
inf ker lg lim liminf limsup ln log max min sec sin sinh sup tan tanh
""".split())
_CONTROL_SYMBOLS = frozenset("%$#&{}_ |,;:!/>\"'.^`~")
_COMMANDS = _SYMBOLS | _PRESENTATION | _CONTROL_SYMBOLS
_TOKEN = re.compile(r"\\([A-Za-z]+|.)|([{}])", re.S)


def check_math_source(expression: str) -> None:
    """Reject unsafe syntax without decoding or rewriting the authored source."""
    if not expression.strip():
        raise ValueError("Empty LaTeX expression")
    if len(expression) > 8000:
        raise ValueError("LaTeX expression exceeds the rendering length limit")
    if not expression.isascii() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", expression):
        raise ValueError("Use ASCII LaTeX commands without control characters")
    if "^^" in expression:
        raise ValueError("TeX character escapes are not supported")
    if re.search(r"(?<!\\)[%&#$]", expression):
        raise ValueError("Escape literal %, &, #, and $ inside math")
    depth = 0
    for match in _TOKEN.finditer(expression):
        command, brace = match.groups()
        if command is not None:
            if command not in _COMMANDS:
                raise ValueError(f"Unsupported LaTeX command: \\{command}")
        elif brace == "{":
            depth += 1
            if depth > 64:
                raise ValueError("LaTeX grouping exceeds the rendering depth limit")
        else:
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced braces in math expression")
    if depth or expression.endswith("\\"):
        raise ValueError("Unbalanced or incomplete LaTeX expression")
