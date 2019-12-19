def legal_move(board_str, mov):  # works!
    '''
        board_str: a string represented by 1 (occupied hole)
        and 0 (empty hole)
        mov: a tuple
        This function checks if peg at index 1 can be placed at index -1.
    '''
    #print("test")
    # after seeing 14, 13, 12, this can work because I saw it as a string, NOT
    # a triangle. I took it as a pythonic statement, not a board.
    # is the first peg occupied?
    # 1 1 0 works too
    # 0 1 1 works too
    if board_str[int(mov[0])] == '1':
        # is the second peg empty?
        if board_str[int(mov[1])] == '1':
            # can the third peg be manipulated?
            if board_str[int(mov[2])] == '0':
                return True
        else:
            # the second peg is not empty.
            return False
    else:
        if board_str[int(mov[0])] == '0':
            # is the second peg empty?
            if board_str[int(mov[1])] == '1':
                # can the third peg be manipulated?
                if board_str[int(mov[2])] == '1':
                    return True
            else:
                # the second peg is not empty.
                return False
        else:
            # the first peg is empty. cannot move a nonexistant peg.
            return False
